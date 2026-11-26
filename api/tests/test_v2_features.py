import base64
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from stellar_sdk import Keypair

from api.models import (
    AgentProfile,
    Exchange,
    ExchangeType,
    ExchangeStatus,
    DisputeStatus,
    DisputeDecision,
)
from api.authentication import generate_auth_challenge, verify_auth_challenge


def _auth_client_for_keypair(kp: Keypair) -> APIClient:
    challenge = generate_auth_challenge(kp.public_key)
    sig = base64.b64encode(kp.sign(challenge['message'].encode('utf-8'))).decode('utf-8')
    token = verify_auth_challenge(kp.public_key, challenge['nonce'], sig)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


class AfriCashV2ApiTest(TestCase):
    def setUp(self):
        self.agent_kp = Keypair.random()
        self.user_kp = Keypair.random()
        self.arbitrator_kp = Keypair.random()

        self.agent = AgentProfile.objects.create(
            stellar_address=self.agent_kp.public_key,
            name="Lagos Liquidity Hub",
            phone="+2348011112222",
            address_text="10 Broad Street, Lagos",
            city="Lagos",
            country="Nigeria",
            supported_currencies=["NGN"],
            exchange_rates={"NGN": "1600.00"},
            available_services=["cash_out", "cash_in"],
            fee_percentage=Decimal('1.00'),
            is_available=True,
            min_usdc_amount=Decimal('10.00'),
            max_usdc_amount=Decimal('500.00'),
            available_liquidity_usdc=Decimal('300.00'),
        )

        self.agent_client = _auth_client_for_keypair(self.agent_kp)
        self.user_client = _auth_client_for_keypair(self.user_kp)
        self.arbitrator_client = _auth_client_for_keypair(self.arbitrator_kp)

    def test_agent_liquidity_management_and_discovery_filters(self):
        liq_url = reverse('agent-liquidity')

        # 1. Agent views current liquidity
        get_res = self.agent_client.get(liq_url)
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(get_res.data['available_liquidity_usdc']), Decimal('300.00'))
        self.assertEqual(Decimal(get_res.data['committed_liquidity_usdc']), Decimal('0.00'))
        self.assertEqual(Decimal(get_res.data['effective_max_usdc_amount']), Decimal('300.00'))
        self.assertIn('CASH_OUT', get_res.data['supported_transaction_types'])
        self.assertIn('CASH_IN', get_res.data['supported_transaction_types'])

        # 2. Agent updates available liquidity
        patch_res = self.agent_client.patch(
            liq_url,
            {"available_liquidity_usdc": "450.00", "note": "Replenished vault"},
            format='json',
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(patch_res.data['available_liquidity_usdc']), Decimal('450.00'))
        self.assertEqual(len(patch_res.data['recent_logs']), 1)

        # 3. Public discovery filters by amount, min_liquidity, and transaction_type
        list_url = reverse('agent-list')
        res_ok = self.client.get(f"{list_url}?transaction_type=CASH_IN&amount=400")
        self.assertEqual(res_ok.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_ok.data), 1)
        self.assertEqual(Decimal(res_ok.data[0]['available_liquidity_usdc']), Decimal('450.00'))

        res_too_high = self.client.get(f"{list_url}?amount=475")
        self.assertEqual(res_too_high.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_too_high.data), 0)

    def test_request_exceeding_available_capacity_is_rejected(self):
        # Agent has 300 USDC available (even though max_usdc_amount is 500)
        over_res = self.user_client.post(
            reverse('exchange-list'),
            {
                "agent_id": self.agent.id,
                "transaction_type": ExchangeType.CASH_OUT,
                "usdc_amount": "350.00",
                "fiat_currency": "NGN",
            },
            format='json',
        )
        self.assertEqual(over_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("usdc_amount", over_res.data)

        # First valid request commits 200 USDC -> 100 USDC remains available
        ok_res = self.user_client.post(
            reverse('exchange-list'),
            {
                "agent_id": self.agent.id,
                "transaction_type": ExchangeType.CASH_OUT,
                "usdc_amount": "200.00",
                "fiat_currency": "NGN",
            },
            format='json',
        )
        self.assertEqual(ok_res.status_code, status.HTTP_201_CREATED)

        self.agent.refresh_from_db()
        self.assertEqual(self.agent.available_liquidity_usdc, Decimal('100.000000'))
        self.assertEqual(self.agent.committed_liquidity_usdc, Decimal('200.000000'))

        # Second request for 150 USDC exceeds remaining 100 USDC -> rejected
        over_res_2 = self.user_client.post(
            reverse('exchange-list'),
            {
                "agent_id": self.agent.id,
                "transaction_type": ExchangeType.CASH_OUT,
                "usdc_amount": "150.00",
                "fiat_currency": "NGN",
            },
            format='json',
        )
        self.assertEqual(over_res_2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cash_in_lifecycle_accept_fund_release_and_liquidity_update(self):
        # 1. User creates CASH_IN request for 120 USDC
        create_res = self.user_client.post(
            reverse('exchange-list'),
            {
                "agent_id": self.agent.id,
                "transaction_type": ExchangeType.CASH_IN,
                "usdc_amount": "120.00",
                "fiat_currency": "NGN",
            },
            format='json',
        )
        self.assertEqual(create_res.status_code, status.HTTP_201_CREATED)
        pk = create_res.data['id']
        self.assertEqual(create_res.data['transaction_type'], ExchangeType.CASH_IN)
        self.assertEqual(create_res.data['depositor_address'], self.agent.stellar_address)
        self.assertEqual(create_res.data['recipient_address'], self.user_kp.public_key)

        # 2. User cannot accept; agent accepts
        bad_accept = self.user_client.post(reverse('exchange-accept', args=[pk]))
        self.assertEqual(bad_accept.status_code, status.HTTP_403_FORBIDDEN)

        accept_res = self.agent_client.post(reverse('exchange-accept', args=[pk]))
        self.assertEqual(accept_res.status_code, status.HTTP_200_OK)
        self.assertEqual(accept_res.data['status'], ExchangeStatus.ACCEPTED)
        self.assertIsNotNone(accept_res.data['accepted_at'])

        # 3. For CASH_IN, agent is the depositor who locks USDC in escrow
        user_fund = self.user_client.post(
            reverse('exchange-notify-funded', args=[pk]),
            {"tx_hash": "cashin_lock_tx_111111111111111111111111"},
            format='json',
        )
        self.assertEqual(user_fund.status_code, status.HTTP_403_FORBIDDEN)

        fund_res = self.agent_client.post(
            reverse('exchange-notify-funded', args=[pk]),
            {"tx_hash": "cashin_lock_tx_111111111111111111111111"},
            format='json',
        )
        self.assertEqual(fund_res.status_code, status.HTTP_200_OK)
        self.assertEqual(fund_res.data['status'], ExchangeStatus.FUNDED)

        # 4. Agent confirms cash receipt and releases USDC
        confirm_res = self.agent_client.post(reverse('exchange-confirm-cash', args=[pk]), {}, format='json')
        self.assertEqual(confirm_res.status_code, status.HTTP_200_OK)
        self.assertEqual(confirm_res.data['status'], ExchangeStatus.CASH_CONFIRMED)

        release_res = self.agent_client.post(
            reverse('exchange-notify-released', args=[pk]),
            {"tx_hash": "cashin_release_tx_2222222222222222222222"},
            format='json',
        )
        self.assertEqual(release_res.status_code, status.HTTP_200_OK)
        self.assertEqual(release_res.data['status'], ExchangeStatus.RELEASED)

        # Committed liquidity is settled (0 committed, 180 available)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.available_liquidity_usdc, Decimal('180.000000'))
        self.assertEqual(self.agent.committed_liquidity_usdc, Decimal('0.000000'))

        # Duplicate settlement is rejected
        dup_release = self.agent_client.post(
            reverse('exchange-notify-released', args=[pk]),
            {"tx_hash": "cashin_release_tx_3333333333333333333333"},
            format='json',
        )
        self.assertEqual(dup_release.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_restores_committed_liquidity(self):
        create_res = self.user_client.post(
            reverse('exchange-list'),
            {
                "agent_id": self.agent.id,
                "usdc_amount": "100.00",
                "fiat_currency": "NGN",
            },
            format='json',
        )
        pk = create_res.data['id']
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.available_liquidity_usdc, Decimal('200.000000'))
        self.assertEqual(self.agent.committed_liquidity_usdc, Decimal('100.000000'))

        cancel_res = self.agent_client.post(
            reverse('exchange-cancel', args=[pk]),
            {"reason": "Cash drawer unavailable"},
            format='json',
        )
        self.assertEqual(cancel_res.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_res.data['status'], ExchangeStatus.CANCELLED)

        self.agent.refresh_from_db()
        self.assertEqual(self.agent.available_liquidity_usdc, Decimal('300.000000'))
        self.assertEqual(self.agent.committed_liquidity_usdc, Decimal('0.000000'))

    def test_dispute_blocks_normal_release_and_arbitrator_resolves_refund(self):
        with override_settings(DISPUTE_ARBITRATOR_ADDRESSES=[self.arbitrator_kp.public_key]):
            # 1. Create, accept, and fund exchange
            create_res = self.user_client.post(
                reverse('exchange-list'),
                {"agent_id": self.agent.id, "usdc_amount": "80.00", "fiat_currency": "NGN"},
                format='json',
            )
            pk = create_res.data['id']
            self.agent_client.post(reverse('exchange-accept', args=[pk]))
            self.user_client.post(
                reverse('exchange-notify-funded', args=[pk]),
                {"tx_hash": "lock_tx_dispute_11111111111111111111"},
                format='json',
            )

            # 2. User raises dispute with evidence references
            dispute_res = self.user_client.post(
                reverse('exchange-dispute', args=[pk]),
                {
                    "reason": "Agent handed counterfeit notes",
                    "evidence_references": ["https://example.com/receipt-1.jpg", "ref:POS-991"],
                    "tx_hash": "dispute_tx_222222222222222222222222",
                },
                format='json',
            )
            self.assertEqual(dispute_res.status_code, status.HTTP_200_OK)
            self.assertEqual(dispute_res.data['status'], ExchangeStatus.DISPUTED)
            self.assertEqual(dispute_res.data['dispute']['status'], DisputeStatus.OPEN)
            self.assertEqual(len(dispute_res.data['dispute']['evidence_references']), 2)

            # 3. Normal release, confirm-cash, and cancel are all blocked while disputed
            self.assertEqual(
                self.user_client.post(
                    reverse('exchange-notify-released', args=[pk]),
                    {"tx_hash": "blocked_release_tx_33333333333333"},
                    format='json',
                ).status_code,
                status.HTTP_400_BAD_REQUEST,
            )
            self.assertEqual(
                self.user_client.post(reverse('exchange-confirm-cash', args=[pk]), {}, format='json').status_code,
                status.HTTP_400_BAD_REQUEST,
            )
            self.assertEqual(
                self.agent_client.post(reverse('exchange-cancel', args=[pk]), {}, format='json').status_code,
                status.HTTP_400_BAD_REQUEST,
            )

            # 4. Regular user/agent cannot resolve dispute (403 Forbidden)
            unauth_resolve = self.user_client.post(
                reverse('exchange-resolve-dispute', args=[pk]),
                {
                    "decision": DisputeDecision.REFUND,
                    "resolution_notes": "Self-resolving",
                    "tx_hash": "unauth_resolve_tx_44444444444444",
                },
                format='json',
            )
            self.assertEqual(unauth_resolve.status_code, status.HTTP_403_FORBIDDEN)

            # 5. Arbitrator lists disputes and resolves to REFUND
            disputes_list = self.arbitrator_client.get(reverse('dispute-list'))
            self.assertEqual(disputes_list.status_code, status.HTTP_200_OK)
            self.assertEqual(len(disputes_list.data), 1)

            resolve_res = self.arbitrator_client.post(
                reverse('exchange-resolve-dispute', args=[pk]),
                {
                    "decision": DisputeDecision.REFUND,
                    "resolution_notes": "Verified user evidence; refunded on-chain.",
                    "tx_hash": "resolve_refund_tx_5555555555555555",
                },
                format='json',
            )
            self.assertEqual(resolve_res.status_code, status.HTTP_200_OK)
            self.assertEqual(resolve_res.data['status'], ExchangeStatus.REFUNDED)
            self.assertEqual(resolve_res.data['dispute']['status'], DisputeStatus.RESOLVED_REFUND)
            self.assertEqual(resolve_res.data['dispute']['resolution_decision'], DisputeDecision.REFUND)

            # Refund restored agent's committed liquidity back to 300.00
            self.agent.refresh_from_db()
            self.assertEqual(self.agent.available_liquidity_usdc, Decimal('300.000000'))
            self.assertEqual(self.agent.committed_liquidity_usdc, Decimal('0.000000'))

            # Duplicate resolution is rejected
            dup_resolve = self.arbitrator_client.post(
                reverse('exchange-resolve-dispute', args=[pk]),
                {
                    "decision": DisputeDecision.RELEASE,
                    "tx_hash": "resolve_dup_tx_666666666666666666",
                },
                format='json',
            )
            self.assertEqual(dup_resolve.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('api.stellar_verifier.verify_horizon_transaction')
    def test_verify_exchange_transactions_endpoint(self, mock_verify):
        mock_verify.return_value = {
            "tx_hash": "verified_lock_tx_1234567890abcdef",
            "verified": True,
            "successful": True,
            "ledger": 999123,
            "created_at": "2026-11-26T10:00:00Z",
            "source_account": self.user_kp.public_key,
            "explorer_url": "https://stellar.expert/explorer/testnet/tx/verified_lock_tx_1234567890abcdef",
            "error": None,
        }

        create_res = self.user_client.post(
            reverse('exchange-list'),
            {"agent_id": self.agent.id, "usdc_amount": "50.00", "fiat_currency": "NGN"},
            format='json',
        )
        pk = create_res.data['id']
        self.user_client.post(
            reverse('exchange-notify-funded', args=[pk]),
            {"tx_hash": "verified_lock_tx_1234567890abcdef"},
            format='json',
        )

        verify_res = self.user_client.get(reverse('exchange-verify', args=[pk]))
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertTrue(verify_res.data['all_verified'])
        self.assertEqual(verify_res.data['verified_transactions'], 1)
        self.assertIn('lock', verify_res.data['explorer_links'])
        self.assertTrue(
            verify_res.data['explorer_links']['lock'].endswith('verified_lock_tx_1234567890abcdef')
        )

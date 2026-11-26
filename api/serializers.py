import hashlib
import secrets
import time
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from api.models import (
    AgentProfile,
    AgentLiquidityLog,
    LiquidityEventType,
    Exchange,
    ExchangeType,
    ExchangeStatus,
    AgentReview,
    UserProfile,
    Dispute,
    DisputeDecision,
)
from api.security import validate_stellar_address, sanitize_text
from api.stellar_verifier import build_exchange_explorer_links, get_explorer_tx_url


class WalletChallengeRequestSerializer(serializers.Serializer):
    stellar_address = serializers.CharField(max_length=56, min_length=56)

    def validate_stellar_address(self, value):
        if not validate_stellar_address(value):
            raise serializers.ValidationError("Invalid Stellar Ed25519 public key format.")
        return value


class WalletChallengeResponseSerializer(serializers.Serializer):
    stellar_address = serializers.CharField()
    nonce = serializers.CharField()
    timestamp = serializers.IntegerField()
    message = serializers.CharField()
    expires_in = serializers.IntegerField()


class WalletVerifyRequestSerializer(serializers.Serializer):
    stellar_address = serializers.CharField(max_length=56, min_length=56)
    nonce = serializers.CharField(max_length=64)
    signature = serializers.CharField(max_length=256)

    def validate_stellar_address(self, value):
        if not validate_stellar_address(value):
            raise serializers.ValidationError("Invalid Stellar Ed25519 public key format.")
        return value


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = [
            'stellar_address',
            'display_name',
            'avatar_url',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['stellar_address', 'created_at', 'updated_at']

    def validate_display_name(self, value):
        return sanitize_text(value, max_length=100)

    def validate_avatar_url(self, value):
        return sanitize_text(value, max_length=500)


class WalletVerifyResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    stellar_address = serializers.CharField()
    display_name = serializers.CharField(allow_blank=True)
    avatar_url = serializers.CharField(allow_blank=True)
    is_agent = serializers.BooleanField()


class WalletMeResponseSerializer(serializers.Serializer):
    stellar_address = serializers.CharField()
    display_name = serializers.CharField(allow_blank=True)
    avatar_url = serializers.CharField(allow_blank=True)
    is_agent = serializers.BooleanField()
    agent_id = serializers.IntegerField(allow_null=True)


class WalletMeUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    avatar_url = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_display_name(self, value):
        return sanitize_text(value, max_length=100)

    def validate_avatar_url(self, value):
        return sanitize_text(value, max_length=500)


class AgentReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentReview
        fields = [
            'id',
            'agent_id',
            'user_address',
            'rating',
            'comment',
            'created_at',
        ]
        read_only_fields = ['id', 'agent_id', 'user_address', 'created_at']

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_comment(self, value):
        return sanitize_text(value, max_length=500)


class AgentLiquidityLogSerializer(serializers.ModelSerializer):
    exchange_id = serializers.IntegerField(source='exchange.exchange_id', read_only=True, allow_null=True)

    class Meta:
        model = AgentLiquidityLog
        fields = [
            'id',
            'exchange_id',
            'event_type',
            'amount_delta',
            'available_after',
            'committed_after',
            'note',
            'created_at',
        ]
        read_only_fields = fields


class AgentLiquidityUpdateSerializer(serializers.Serializer):
    available_liquidity_usdc = serializers.DecimalField(
        max_digits=18, decimal_places=6, min_value=Decimal('0.000000')
    )
    available_services = serializers.ListField(
        child=serializers.CharField(max_length=32),
        required=False,
    )
    is_available = serializers.BooleanField(required=False)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_note(self, value):
        return sanitize_text(value, max_length=255)


class AgentLiquidityResponseSerializer(serializers.Serializer):
    agent_id = serializers.IntegerField()
    stellar_address = serializers.CharField()
    available_liquidity_usdc = serializers.DecimalField(max_digits=18, decimal_places=6)
    committed_liquidity_usdc = serializers.DecimalField(max_digits=18, decimal_places=6)
    total_liquidity_usdc = serializers.DecimalField(max_digits=18, decimal_places=6)
    effective_max_usdc_amount = serializers.DecimalField(max_digits=18, decimal_places=6)
    supported_transaction_types = serializers.ListField(child=serializers.CharField())
    is_available = serializers.BooleanField()
    liquidity_updated_at = serializers.DateTimeField(allow_null=True)
    recent_logs = AgentLiquidityLogSerializer(many=True)


class AgentProfileSerializer(serializers.ModelSerializer):
    review_count = serializers.IntegerField(source='reviews.count', read_only=True)
    total_liquidity_usdc = serializers.DecimalField(
        max_digits=18, decimal_places=6, read_only=True
    )
    effective_max_usdc_amount = serializers.DecimalField(
        max_digits=18, decimal_places=6, read_only=True
    )
    supported_transaction_types = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )

    class Meta:
        model = AgentProfile
        fields = [
            'id',
            'stellar_address',
            'name',
            'avatar_url',
            'phone',
            'email',
            'address_text',
            'city',
            'country',
            'latitude',
            'longitude',
            'supported_currencies',
            'exchange_rates',
            'available_services',
            'supported_transaction_types',
            'handover_methods',
            'operating_hours',
            'fee_percentage',
            'is_available',
            'is_verified',
            'reputation_score',
            'completed_trades',
            'review_count',
            'min_usdc_amount',
            'max_usdc_amount',
            'available_liquidity_usdc',
            'committed_liquidity_usdc',
            'total_liquidity_usdc',
            'effective_max_usdc_amount',
            'liquidity_updated_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'reputation_score',
            'completed_trades',
            'review_count',
            'committed_liquidity_usdc',
            'total_liquidity_usdc',
            'effective_max_usdc_amount',
            'supported_transaction_types',
            'liquidity_updated_at',
            'created_at',
            'updated_at',
        ]

    def validate_stellar_address(self, value):
        if not validate_stellar_address(value):
            raise serializers.ValidationError("Invalid Stellar address.")
        return value

    def validate_name(self, value):
        cleaned = sanitize_text(value, max_length=150)
        if not cleaned:
            raise serializers.ValidationError("Name cannot be empty.")
        return cleaned

    def validate_avatar_url(self, value):
        return sanitize_text(value, max_length=500)

    def validate_address_text(self, value):
        return sanitize_text(value, max_length=255)

    def validate_city(self, value):
        return sanitize_text(value, max_length=100)

    def validate_country(self, value):
        return sanitize_text(value, max_length=100)

    def validate_operating_hours(self, value):
        return sanitize_text(value, max_length=100)

    def validate_available_liquidity_usdc(self, value):
        if value < Decimal('0'):
            raise serializers.ValidationError("available_liquidity_usdc cannot be negative.")
        return value

    def validate_exchange_rates(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("exchange_rates must be a dictionary of currency to rate.")
        for curr, rate in value.items():
            try:
                rate_dec = Decimal(str(rate))
                if rate_dec <= Decimal('0'):
                    raise serializers.ValidationError(f"Rate for {curr} must be greater than 0.")
            except Exception:
                raise serializers.ValidationError(f"Invalid numeric rate format for {curr}.")
        return value

    def validate(self, attrs):
        min_amt = attrs.get('min_usdc_amount', getattr(self.instance, 'min_usdc_amount', Decimal('5.00')))
        max_amt = attrs.get('max_usdc_amount', getattr(self.instance, 'max_usdc_amount', Decimal('1000.00')))
        if min_amt > max_amt:
            raise serializers.ValidationError("min_usdc_amount cannot be greater than max_usdc_amount.")
        return attrs


class DisputeSerializer(serializers.ModelSerializer):
    exchange_pk = serializers.IntegerField(source='exchange.id', read_only=True)
    exchange_id = serializers.IntegerField(source='exchange.exchange_id', read_only=True)
    exchange_status = serializers.CharField(source='exchange.status', read_only=True)
    transaction_type = serializers.CharField(source='exchange.transaction_type', read_only=True)
    usdc_amount = serializers.DecimalField(
        source='exchange.usdc_amount', max_digits=18, decimal_places=6, read_only=True
    )
    dispute_explorer_url = serializers.SerializerMethodField()
    resolution_explorer_url = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_dispute_explorer_url(self, obj):
        return get_explorer_tx_url(obj.dispute_tx_hash)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_resolution_explorer_url(self, obj):
        return get_explorer_tx_url(obj.resolution_tx_hash)

    class Meta:
        model = Dispute
        fields = [
            'id',
            'exchange_pk',
            'exchange_id',
            'exchange_status',
            'transaction_type',
            'usdc_amount',
            'opened_by',
            'opened_by_role',
            'reason',
            'evidence_references',
            'status',
            'dispute_tx_hash',
            'dispute_explorer_url',
            'resolution_decision',
            'resolution_notes',
            'resolved_by',
            'resolution_tx_hash',
            'resolution_explorer_url',
            'created_at',
            'updated_at',
            'resolved_at',
        ]
        read_only_fields = fields


class ExchangeCreateSerializer(serializers.Serializer):
    agent_id = serializers.IntegerField()
    transaction_type = serializers.ChoiceField(
        choices=ExchangeType.choices,
        default=ExchangeType.CASH_OUT,
        required=False,
    )
    usdc_amount = serializers.DecimalField(max_digits=18, decimal_places=6, min_value=Decimal('0.000001'))
    fiat_currency = serializers.CharField(max_length=10)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_fiat_currency(self, value):
        return value.strip().upper()

    def validate_transaction_type(self, value):
        return value.strip().upper()

    def validate(self, attrs):
        agent_id = attrs.get('agent_id')
        transaction_type = attrs.get('transaction_type', ExchangeType.CASH_OUT)
        usdc_amount = attrs.get('usdc_amount')
        fiat_currency = attrs.get('fiat_currency')
        request = self.context.get('request')
        user_address = getattr(request.user, 'stellar_address', request.user.username) if request else ''

        try:
            agent = AgentProfile.objects.get(id=agent_id)
        except AgentProfile.DoesNotExist:
            raise serializers.ValidationError({"agent_id": "Agent does not exist."})

        if user_address and agent.stellar_address == user_address:
            raise serializers.ValidationError({"agent_id": "You cannot create an exchange with yourself."})

        if not agent.is_available:
            raise serializers.ValidationError({"agent_id": "This agent is currently marked as unavailable."})

        # Verify agent supports the requested transaction type
        services_lower = [str(s).lower() for s in (agent.available_services or [])]
        required_service = 'cash_in' if transaction_type == ExchangeType.CASH_IN else 'cash_out'
        if services_lower and required_service not in services_lower:
            raise serializers.ValidationError({
                "transaction_type": f"This agent does not support {transaction_type} requests."
            })

        if usdc_amount < agent.min_usdc_amount or usdc_amount > agent.max_usdc_amount:
            raise serializers.ValidationError({
                "usdc_amount": f"Amount must be between {agent.min_usdc_amount} and {agent.max_usdc_amount} USDC."
            })

        # v2.0 Capacity Enforcement: reject requests exceeding available agent liquidity
        if usdc_amount > agent.available_liquidity_usdc:
            raise serializers.ValidationError({
                "usdc_amount": (
                    f"Requested amount ({usdc_amount} USDC) exceeds agent's available liquidity "
                    f"capacity ({agent.available_liquidity_usdc} USDC)."
                )
            })

        if fiat_currency not in agent.supported_currencies:
            raise serializers.ValidationError({
                "fiat_currency": f"Currency {fiat_currency} is not supported by this agent. Supported: {', '.join(agent.supported_currencies)}"
            })

        rate_str = agent.exchange_rates.get(fiat_currency)
        if not rate_str:
            raise serializers.ValidationError({
                "fiat_currency": f"Exchange rate not configured for {fiat_currency}."
            })

        try:
            rate = Decimal(str(rate_str))
            if rate <= Decimal('0'):
                raise ValueError
        except Exception:
            raise serializers.ValidationError({"fiat_currency": "Invalid agent exchange rate configuration."})

        attrs['agent'] = agent
        attrs['transaction_type'] = transaction_type
        attrs['exchange_rate'] = rate
        # Calculate fiat payout: usdc_amount * rate
        attrs['fiat_amount'] = (usdc_amount * rate).quantize(Decimal('0.01'))
        return attrs

    def create(self, validated_data):
        agent_ref = validated_data['agent']
        transaction_type = validated_data['transaction_type']
        usdc_amount = validated_data['usdc_amount']
        fiat_currency = validated_data['fiat_currency']
        exchange_rate = validated_data['exchange_rate']
        fiat_amount = validated_data['fiat_amount']
        notes = sanitize_text(validated_data.get('notes', ''))
        request = self.context['request']
        user_address = getattr(request.user, 'stellar_address', request.user.username)

        # Unique 64-bit exchange ID for Soroban contract compatibility
        exchange_id = int(time.time() * 1000) * 1000 + secrets.randbelow(1000)

        # Generate 6-digit OTP secret for cash handover verification
        secret_plain = f"{secrets.randbelow(900000) + 100000}"
        secret_hash = hashlib.sha256(secret_plain.encode('utf-8')).hexdigest()

        # Default timeout: 2 hours (7200 seconds)
        timeout_timestamp = int(time.time()) + 7200

        with transaction.atomic():
            agent = AgentProfile.objects.select_for_update().get(id=agent_ref.id)
            if not agent.is_available:
                raise serializers.ValidationError({"agent_id": "This agent is currently marked as unavailable."})
            if usdc_amount > agent.available_liquidity_usdc:
                raise serializers.ValidationError({
                    "usdc_amount": (
                        f"Requested amount ({usdc_amount} USDC) exceeds agent's available liquidity "
                        f"capacity ({agent.available_liquidity_usdc} USDC)."
                    )
                })

            agent.available_liquidity_usdc -= usdc_amount
            agent.committed_liquidity_usdc += usdc_amount
            agent.liquidity_updated_at = timezone.now()
            agent.save(update_fields=[
                'available_liquidity_usdc',
                'committed_liquidity_usdc',
                'liquidity_updated_at',
                'updated_at',
            ])

            exchange = Exchange.objects.create(
                exchange_id=exchange_id,
                transaction_type=transaction_type,
                user_address=user_address,
                agent=agent,
                usdc_amount=usdc_amount,
                fiat_currency=fiat_currency,
                exchange_rate=exchange_rate,
                fiat_amount=fiat_amount,
                secret=secret_plain,
                secret_hash=secret_hash,
                timeout_timestamp=timeout_timestamp,
                notes=notes,
                status=ExchangeStatus.INITIATED,
                liquidity_committed=True,
            )

            AgentLiquidityLog.objects.create(
                agent=agent,
                exchange=exchange,
                event_type=LiquidityEventType.COMMIT,
                amount_delta=-usdc_amount,
                available_after=agent.available_liquidity_usdc,
                committed_after=agent.committed_liquidity_usdc,
                note=f"Committed liquidity for {transaction_type} Exchange #{exchange_id}",
            )

        return exchange


class ExchangeSerializer(serializers.ModelSerializer):
    agent_name = serializers.CharField(source='agent.name', read_only=True)
    agent_address = serializers.CharField(source='agent.stellar_address', read_only=True)
    agent_avatar_url = serializers.CharField(source='agent.avatar_url', read_only=True)
    agent_phone = serializers.CharField(source='agent.phone', read_only=True)
    agent_location = serializers.CharField(source='agent.address_text', read_only=True)
    depositor_address = serializers.CharField(read_only=True)
    recipient_address = serializers.CharField(read_only=True)
    user_display_name = serializers.SerializerMethodField()
    user_avatar_url = serializers.SerializerMethodField()
    explorer_links = serializers.SerializerMethodField()
    review = AgentReviewSerializer(read_only=True)
    dispute = DisputeSerializer(read_only=True)

    @extend_schema_field(serializers.CharField())
    def get_user_display_name(self, obj) -> str:
        profile = UserProfile.objects.filter(stellar_address=obj.user_address).first()
        return profile.display_name if profile else ''

    @extend_schema_field(serializers.CharField())
    def get_user_avatar_url(self, obj) -> str:
        profile = UserProfile.objects.filter(stellar_address=obj.user_address).first()
        return profile.avatar_url if profile else ''

    @extend_schema_field(serializers.DictField(child=serializers.CharField(allow_null=True)))
    def get_explorer_links(self, obj) -> dict:
        return build_exchange_explorer_links(obj)

    class Meta:
        model = Exchange
        fields = [
            'id',
            'exchange_id',
            'transaction_type',
            'user_address',
            'user_display_name',
            'user_avatar_url',
            'agent_id',
            'agent_name',
            'agent_address',
            'agent_avatar_url',
            'agent_phone',
            'agent_location',
            'depositor_address',
            'recipient_address',
            'usdc_amount',
            'fiat_currency',
            'exchange_rate',
            'fiat_amount',
            'secret',
            'secret_hash',
            'status',
            'escrow_contract_id',
            'token_address',
            'lock_tx_hash',
            'release_tx_hash',
            'refund_tx_hash',
            'dispute_tx_hash',
            'resolution_tx_hash',
            'explorer_links',
            'timeout_timestamp',
            'cancellation_reason',
            'notes',
            'liquidity_committed',
            'accepted_at',
            'settled_at',
            'review',
            'dispute',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class NotifyFundedSerializer(serializers.Serializer):
    tx_hash = serializers.CharField(max_length=66, min_length=10)
    contract_id = serializers.CharField(max_length=64, required=False, allow_blank=True)

    def validate_tx_hash(self, value):
        return sanitize_text(value, max_length=66)


class ConfirmCashSerializer(serializers.Serializer):
    secret = serializers.CharField(max_length=64, required=False, allow_blank=True)


class NotifyReleasedSerializer(serializers.Serializer):
    tx_hash = serializers.CharField(max_length=66, min_length=10)

    def validate_tx_hash(self, value):
        return sanitize_text(value, max_length=66)


class CancelExchangeSerializer(serializers.Serializer):
    tx_hash = serializers.CharField(max_length=66, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_tx_hash(self, value):
        return sanitize_text(value, max_length=66)

    def validate_reason(self, value):
        return sanitize_text(value, max_length=255)


class DisputeExchangeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)
    evidence_references = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
        default=list,
    )
    tx_hash = serializers.CharField(max_length=66, required=False, allow_blank=True)

    def validate_reason(self, value):
        cleaned = sanitize_text(value, max_length=500)
        if not cleaned:
            raise serializers.ValidationError("Dispute reason cannot be empty.")
        return cleaned

    def validate_evidence_references(self, value):
        return [sanitize_text(item, max_length=500) for item in value if sanitize_text(item, max_length=500)]

    def validate_tx_hash(self, value):
        return sanitize_text(value, max_length=66)


class ResolveDisputeSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=DisputeDecision.choices)
    resolution_notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    tx_hash = serializers.CharField(max_length=66, min_length=10)

    def validate_resolution_notes(self, value):
        return sanitize_text(value, max_length=1000)

    def validate_tx_hash(self, value):
        return sanitize_text(value, max_length=66)


class ReviewCreateSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_comment(self, value):
        return sanitize_text(value, max_length=500)


class ConfigSerializer(serializers.Serializer):
    network_passphrase = serializers.CharField()
    rpc_url = serializers.CharField()
    horizon_url = serializers.CharField()
    explorer_base_url = serializers.CharField()
    usdc_contract_id = serializers.CharField()
    escrow_contract_id = serializers.CharField()


class FileUploadRequestSerializer(serializers.Serializer):
    file = serializers.FileField()


class FileUploadResponseSerializer(serializers.Serializer):
    url = serializers.CharField()
    object_key = serializers.CharField()
    content_type = serializers.CharField()
    size = serializers.IntegerField()

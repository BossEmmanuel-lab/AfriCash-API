import time
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, F, Avg
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse

from api.models import (
    AgentProfile,
    AgentLiquidityLog,
    LiquidityEventType,
    Exchange,
    ExchangeType,
    ExchangeStatus,
    AgentReview,
    Dispute,
    DisputeStatus,
    DisputeDecision,
)
from api.storage import upload_object, ALLOWED_IMAGE_TYPES, MAX_FILE_SIZE_BYTES
from api.stellar_verifier import verify_exchange_transactions
from api.serializers import (
    AgentProfileSerializer,
    AgentLiquidityUpdateSerializer,
    AgentLiquidityResponseSerializer,
    AgentLiquidityLogSerializer,
    AgentReviewSerializer,
    DisputeSerializer,
    ExchangeSerializer,
    ExchangeCreateSerializer,
    NotifyFundedSerializer,
    ConfirmCashSerializer,
    NotifyReleasedSerializer,
    CancelExchangeSerializer,
    DisputeExchangeSerializer,
    ResolveDisputeSerializer,
    ReviewCreateSerializer,
    ConfigSerializer,
    FileUploadRequestSerializer,
    FileUploadResponseSerializer,
)

CACHE_KEY_AGENTS_PREFIX = "africash_agents_list"
CACHE_TTL_AGENTS = 60  # 1 minute

SETTLED_STATUSES = (
    ExchangeStatus.RELEASED,
    ExchangeStatus.REFUNDED,
    ExchangeStatus.CANCELLED,
)


def invalidate_agents_cache():
    # Clear cached agent discovery responses (supports LocMemCache & Redis)
    try:
        cache.delete_pattern(f"{CACHE_KEY_AGENTS_PREFIX}_*")
    except AttributeError:
        cache.delete_many([
            f"{CACHE_KEY_AGENTS_PREFIX}_all",
            f"{CACHE_KEY_AGENTS_PREFIX}_available",
        ])
        # For LocMemCache, clear agent list keys by bumping a version token
        version = cache.get(f"{CACHE_KEY_AGENTS_PREFIX}_ver", 0)
        cache.set(f"{CACHE_KEY_AGENTS_PREFIX}_ver", version + 1, timeout=None)


def is_authorized_arbitrator(request) -> bool:
    user = request.user
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True
    stellar_address = getattr(user, 'stellar_address', user.username)
    arbitrators = getattr(settings, 'DISPUTE_ARBITRATOR_ADDRESSES', [])
    return stellar_address in arbitrators


def _settle_committed_liquidity(exchange: Exchange, note: str):
    """Consume committed liquidity upon successful completion (RELEASED)."""
    if not exchange.liquidity_committed:
        return
    agent = AgentProfile.objects.select_for_update().get(id=exchange.agent_id)
    deduct = min(agent.committed_liquidity_usdc, exchange.usdc_amount)
    agent.committed_liquidity_usdc -= deduct
    agent.liquidity_updated_at = timezone.now()
    agent.save(update_fields=['committed_liquidity_usdc', 'liquidity_updated_at', 'updated_at'])

    exchange.liquidity_committed = False
    AgentLiquidityLog.objects.create(
        agent=agent,
        exchange=exchange,
        event_type=LiquidityEventType.SETTLE,
        amount_delta=Decimal('0.000000'),
        available_after=agent.available_liquidity_usdc,
        committed_after=agent.committed_liquidity_usdc,
        note=note,
    )


def _release_committed_liquidity(exchange: Exchange, note: str):
    """Restore committed liquidity back to available upon cancellation or refund."""
    if not exchange.liquidity_committed:
        return
    agent = AgentProfile.objects.select_for_update().get(id=exchange.agent_id)
    restore = min(agent.committed_liquidity_usdc, exchange.usdc_amount)
    agent.committed_liquidity_usdc -= restore
    agent.available_liquidity_usdc += restore
    agent.liquidity_updated_at = timezone.now()
    agent.save(update_fields=[
        'available_liquidity_usdc',
        'committed_liquidity_usdc',
        'liquidity_updated_at',
        'updated_at',
    ])

    exchange.liquidity_committed = False
    AgentLiquidityLog.objects.create(
        agent=agent,
        exchange=exchange,
        event_type=LiquidityEventType.RELEASE_COMMIT,
        amount_delta=restore,
        available_after=agent.available_liquidity_usdc,
        committed_after=agent.committed_liquidity_usdc,
        note=note,
    )


class AgentViewSet(viewsets.ModelViewSet):
    serializer_class = AgentProfileSerializer
    queryset = AgentProfile.objects.all().order_by('-reputation_score', '-completed_trades')

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'reviews']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    @extend_schema(
        summary="List and discover available cash agents",
        description=(
            "Returns cash agent profiles with capacity and liquidity visibility. "
            "Supports filtering by search query, fiat currency, city, availability, verification, "
            "transaction type (cash_in / cash_out), and required USDC amount/minimum liquidity."
        ),
        parameters=[
            OpenApiParameter("search", str, description="Search by agent name, city, or street address"),
            OpenApiParameter("currency", str, description="Filter by supported fiat currency (e.g. NGN, GHS, KES)"),
            OpenApiParameter("city", str, description="Filter by city name"),
            OpenApiParameter("available", bool, description="Filter by online availability (true/false)"),
            OpenApiParameter("verified", bool, description="Filter by verification badge (true/false)"),
            OpenApiParameter("transaction_type", str, description="Filter by supported type: CASH_OUT or CASH_IN"),
            OpenApiParameter("min_liquidity", str, description="Filter agents with available_liquidity_usdc >= value"),
            OpenApiParameter("amount", str, description="Filter eligible agents able to serve this USDC amount"),
        ],
        tags=["Cash Agents"],
    )
    def list(self, request, *args, **kwargs):
        currency = request.query_params.get('currency', '').strip().upper()
        city = request.query_params.get('city', '').strip()
        search = (request.query_params.get('search') or request.query_params.get('q') or '').strip()
        available_param = request.query_params.get('available')
        verified_param = request.query_params.get('verified')
        tx_type = request.query_params.get('transaction_type', '').strip().lower()
        min_liquidity = request.query_params.get('min_liquidity', '').strip()
        amount_param = request.query_params.get('amount', '').strip()

        cache_ver = cache.get(f"{CACHE_KEY_AGENTS_PREFIX}_ver", 0)
        cache_key = (
            f"{CACHE_KEY_AGENTS_PREFIX}_v{cache_ver}_{currency}_{city}_"
            f"{available_param}_{verified_param}_{tx_type}_{min_liquidity}_{amount_param}_{search}"
        )
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        queryset = self.filter_queryset(self.get_queryset())

        if currency:
            queryset = queryset.filter(supported_currencies__icontains=currency)
        if city:
            queryset = queryset.filter(city__icontains=city)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(city__icontains=search) |
                Q(address_text__icontains=search)
            )
        if available_param is not None:
            is_avail = available_param.lower() in ['true', '1']
            queryset = queryset.filter(is_available=is_avail)
        if verified_param is not None:
            is_ver = verified_param.lower() in ['true', '1']
            queryset = queryset.filter(is_verified=is_ver)
        if tx_type in ('cash_out', 'cash_in'):
            queryset = queryset.filter(available_services__icontains=tx_type)

        if min_liquidity:
            try:
                min_liq_dec = Decimal(min_liquidity)
                queryset = queryset.filter(available_liquidity_usdc__gte=min_liq_dec)
            except (InvalidOperation, ValueError):
                return Response(
                    {"error": "Invalid min_liquidity decimal value."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if amount_param:
            try:
                amt_dec = Decimal(amount_param)
                queryset = queryset.filter(
                    is_available=True,
                    min_usdc_amount__lte=amt_dec,
                    max_usdc_amount__gte=amt_dec,
                    available_liquidity_usdc__gte=amt_dec,
                )
            except (InvalidOperation, ValueError):
                return Response(
                    {"error": "Invalid amount decimal value."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data
        cache.set(cache_key, data, timeout=CACHE_TTL_AGENTS)
        return Response(data)

    @extend_schema(
        summary="Register as a cash agent",
        description="Creates a new cash agent profile bound to the authenticated Stellar wallet address, including initial available liquidity.",
        tags=["Cash Agents"],
    )
    def create(self, request, *args, **kwargs):
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)
        if AgentProfile.objects.filter(stellar_address=stellar_address).exists():
            return Response(
                {"error": "An agent profile already exists for this wallet address."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = request.data.copy()
        data['stellar_address'] = stellar_address

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        agent = serializer.save()

        if agent.available_liquidity_usdc > Decimal('0'):
            agent.liquidity_updated_at = timezone.now()
            agent.save(update_fields=['liquidity_updated_at'])
            AgentLiquidityLog.objects.create(
                agent=agent,
                event_type=LiquidityEventType.REGISTER,
                amount_delta=agent.available_liquidity_usdc,
                available_after=agent.available_liquidity_usdc,
                committed_after=agent.committed_liquidity_usdc,
                note="Initial agent liquidity registration",
            )

        invalidate_agents_cache()
        return Response(self.get_serializer(agent).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Get or update own agent profile",
        description="Retrieve or modify availability, fees, operating hours, liquidity, or rates for the currently authenticated agent.",
        tags=["Cash Agents"],
    )
    @action(detail=False, methods=['get', 'patch', 'put'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)
        try:
            agent = AgentProfile.objects.get(stellar_address=stellar_address)
        except AgentProfile.DoesNotExist:
            return Response(
                {"error": "No agent profile found for this wallet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == 'GET':
            serializer = self.get_serializer(agent)
            return Response(serializer.data)

        old_liquidity = agent.available_liquidity_usdc
        serializer = self.get_serializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        agent = serializer.save()

        if agent.available_liquidity_usdc != old_liquidity:
            agent.liquidity_updated_at = timezone.now()
            agent.save(update_fields=['liquidity_updated_at'])
            AgentLiquidityLog.objects.create(
                agent=agent,
                event_type=LiquidityEventType.UPDATE,
                amount_delta=agent.available_liquidity_usdc - old_liquidity,
                available_after=agent.available_liquidity_usdc,
                committed_after=agent.committed_liquidity_usdc,
                note="Updated via agent profile",
            )

        invalidate_agents_cache()
        return Response(self.get_serializer(agent).data)

    @extend_schema(
        summary="Manage authenticated agent liquidity",
        description=(
            "GET returns available, committed, and total USDC liquidity with recent ledger logs. "
            "POST/PATCH registers or updates available liquidity and optionally supported services / availability."
        ),
        request=AgentLiquidityUpdateSerializer,
        responses={200: AgentLiquidityResponseSerializer},
        tags=["Cash Agents"],
    )
    @action(detail=False, methods=['get', 'post', 'patch'], permission_classes=[permissions.IsAuthenticated])
    def liquidity(self, request):
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)
        try:
            agent = AgentProfile.objects.get(stellar_address=stellar_address)
        except AgentProfile.DoesNotExist:
            return Response(
                {"error": "No agent profile found for this wallet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method in ('POST', 'PATCH'):
            req_serializer = AgentLiquidityUpdateSerializer(data=request.data)
            req_serializer.is_valid(raise_exception=True)
            new_available = req_serializer.validated_data['available_liquidity_usdc']
            note = req_serializer.validated_data.get('note', '')

            with transaction.atomic():
                agent = AgentProfile.objects.select_for_update().get(id=agent.id)
                old_available = agent.available_liquidity_usdc
                event_type = (
                    LiquidityEventType.REGISTER
                    if agent.liquidity_updated_at is None
                    else LiquidityEventType.UPDATE
                )
                agent.available_liquidity_usdc = new_available
                agent.liquidity_updated_at = timezone.now()

                update_fields = ['available_liquidity_usdc', 'liquidity_updated_at', 'updated_at']
                if 'available_services' in req_serializer.validated_data:
                    agent.available_services = req_serializer.validated_data['available_services']
                    update_fields.append('available_services')
                if 'is_available' in req_serializer.validated_data:
                    agent.is_available = req_serializer.validated_data['is_available']
                    update_fields.append('is_available')

                agent.save(update_fields=update_fields)
                AgentLiquidityLog.objects.create(
                    agent=agent,
                    event_type=event_type,
                    amount_delta=new_available - old_available,
                    available_after=agent.available_liquidity_usdc,
                    committed_after=agent.committed_liquidity_usdc,
                    note=note or f"Agent set available liquidity to {new_available} USDC",
                )

            invalidate_agents_cache()

        recent_logs = agent.liquidity_logs.all()[:20]
        return Response({
            "agent_id": agent.id,
            "stellar_address": agent.stellar_address,
            "available_liquidity_usdc": str(agent.available_liquidity_usdc),
            "committed_liquidity_usdc": str(agent.committed_liquidity_usdc),
            "total_liquidity_usdc": str(agent.total_liquidity_usdc),
            "effective_max_usdc_amount": str(agent.effective_max_usdc_amount),
            "supported_transaction_types": agent.supported_transaction_types,
            "is_available": agent.is_available,
            "liquidity_updated_at": agent.liquidity_updated_at,
            "recent_logs": AgentLiquidityLogSerializer(recent_logs, many=True).data,
        })

    @extend_schema(
        summary="Get agent dashboard statistics",
        description="Returns trading and liquidity statistics for the currently authenticated agent.",
        tags=["Cash Agents"],
    )
    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def stats(self, request):
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)
        try:
            agent = AgentProfile.objects.get(stellar_address=stellar_address)
        except AgentProfile.DoesNotExist:
            return Response({"error": "No agent profile found for this wallet."}, status=status.HTTP_404_NOT_FOUND)

        completed_exchanges = Exchange.objects.filter(agent=agent, status=ExchangeStatus.RELEASED)
        active_exchanges = Exchange.objects.filter(
            agent=agent,
            status__in=[
                ExchangeStatus.INITIATED,
                ExchangeStatus.ACCEPTED,
                ExchangeStatus.FUNDED,
                ExchangeStatus.CASH_PENDING,
                ExchangeStatus.CASH_CONFIRMED,
            ],
        )
        pending_requests = Exchange.objects.filter(agent=agent, status=ExchangeStatus.INITIATED)
        disputed_exchanges = Exchange.objects.filter(agent=agent, status=ExchangeStatus.DISPUTED)

        total_volume_usdc = sum((e.usdc_amount for e in completed_exchanges), Decimal('0.00'))
        total_earnings_usdc = sum(
            (e.usdc_amount * (agent.fee_percentage / Decimal('100')) for e in completed_exchanges),
            Decimal('0.00'),
        ).quantize(Decimal('0.01'))

        return Response({
            "completed_trades": completed_exchanges.count(),
            "active_trades": active_exchanges.count(),
            "pending_requests": pending_requests.count(),
            "disputed_trades": disputed_exchanges.count(),
            "total_volume_usdc": str(total_volume_usdc),
            "estimated_earnings_usdc": str(total_earnings_usdc),
            "available_liquidity_usdc": str(agent.available_liquidity_usdc),
            "committed_liquidity_usdc": str(agent.committed_liquidity_usdc),
            "total_liquidity_usdc": str(agent.total_liquidity_usdc),
            "effective_max_usdc_amount": str(agent.effective_max_usdc_amount),
            "reputation_score": str(agent.reputation_score),
            "review_count": agent.reviews.count(),
            "is_available": agent.is_available,
        })

    @extend_schema(
        summary="List customer reviews for an agent",
        description="Returns ratings and testimonials left by traders who completed exchanges with this agent.",
        responses={200: AgentReviewSerializer(many=True)},
        tags=["Cash Agents"],
    )
    @action(detail=True, methods=['get'])
    def reviews(self, request, pk=None):
        agent = self.get_object()
        reviews = agent.reviews.all().order_by('-created_at')
        return Response(AgentReviewSerializer(reviews, many=True).data)

    def perform_update(self, serializer):
        serializer.save()
        invalidate_agents_cache()

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_agents_cache()


class ExchangeViewSet(viewsets.ModelViewSet):
    serializer_class = ExchangeSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        stellar_address = getattr(self.request.user, 'stellar_address', self.request.user.username)
        if is_authorized_arbitrator(self.request):
            queryset = Exchange.objects.all()
        else:
            queryset = Exchange.objects.filter(
                Q(user_address=stellar_address) | Q(agent__stellar_address=stellar_address)
            )

        queryset = queryset.select_related('agent', 'review', 'dispute').order_by('-created_at')

        role = self.request.query_params.get('role', '').lower()
        if role == 'user':
            queryset = queryset.filter(user_address=stellar_address)
        elif role == 'agent':
            queryset = queryset.filter(agent__stellar_address=stellar_address)

        tx_type = self.request.query_params.get('transaction_type', '').strip().upper()
        if tx_type in (ExchangeType.CASH_OUT, ExchangeType.CASH_IN):
            queryset = queryset.filter(transaction_type=tx_type)

        status_param = self.request.query_params.get('status')
        if status_param:
            status_param = status_param.strip().upper()
            if status_param == 'ACTIVE':
                queryset = queryset.filter(
                    status__in=[
                        ExchangeStatus.INITIATED,
                        ExchangeStatus.ACCEPTED,
                        ExchangeStatus.FUNDED,
                        ExchangeStatus.CASH_PENDING,
                        ExchangeStatus.CASH_CONFIRMED,
                    ]
                )
            elif status_param == 'COMPLETED':
                queryset = queryset.filter(status=ExchangeStatus.RELEASED)
            elif status_param == 'CANCELLED':
                queryset = queryset.filter(status__in=[ExchangeStatus.CANCELLED, ExchangeStatus.REFUNDED])
            else:
                queryset = queryset.filter(status=status_param)

        return queryset

    @extend_schema(
        summary="Initiate a cash-out or cash-in exchange request",
        description=(
            "Creates a cash-out or cash-in request with an eligible agent, validates capacity against "
            "available liquidity, atomically commits agent liquidity, and generates an OTP secret hash "
            "for the Soroban escrow contract."
        ),
        request=ExchangeCreateSerializer,
        responses={201: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    def create(self, request, *args, **kwargs):
        serializer = ExchangeCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        exchange = serializer.save()

        # Bind configured testnet contract addresses
        exchange.escrow_contract_id = getattr(settings, 'ESCROW_CONTRACT_ID', '')
        exchange.token_address = getattr(settings, 'USDC_ASSET_CONTRACT_ID', '')
        exchange.save(update_fields=['escrow_contract_id', 'token_address'])
        invalidate_agents_cache()

        response_serializer = ExchangeSerializer(exchange)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Accept an incoming exchange request (agent only)",
        description="Allows the assigned agent to accept an INITIATED cash-in or cash-out request, transitioning status to ACCEPTED.",
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        exchange = self.get_object()
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)

        if exchange.agent.stellar_address != stellar_address:
            return Response(
                {"error": "Only the assigned agent can accept this request."},
                status=status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)
            if exchange.status != ExchangeStatus.INITIATED:
                return Response(
                    {"error": f"Cannot accept exchange in status {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            exchange.status = ExchangeStatus.ACCEPTED
            exchange.accepted_at = timezone.now()
            exchange.save(update_fields=['status', 'accepted_at', 'updated_at'])

        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Notify that USDC funds are locked in escrow",
        description=(
            "Called after the depositor (user for CASH_OUT, agent for CASH_IN) signs lock_funds "
            "on the Soroban escrow contract. Transitions status to FUNDED."
        ),
        request=NotifyFundedSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'], url_path='notify-funded')
    def notify_funded(self, request, pk=None):
        exchange = self.get_object()
        caller = getattr(request.user, 'stellar_address', request.user.username)

        if caller != exchange.depositor_address:
            return Response(
                {"error": "Only the escrow depositor can record escrow funding."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = NotifyFundedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tx_hash = serializer.validated_data['tx_hash']
        contract_id = serializer.validated_data.get('contract_id')

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)

            allowed_statuses = (
                [ExchangeStatus.ACCEPTED]
                if exchange.transaction_type == ExchangeType.CASH_IN
                else [ExchangeStatus.INITIATED, ExchangeStatus.ACCEPTED]
            )
            if exchange.status not in allowed_statuses:
                return Response(
                    {"error": f"Cannot fund exchange in status {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if Exchange.objects.filter(lock_tx_hash=tx_hash).exclude(id=exchange.id).exists():
                return Response(
                    {"error": "This funding transaction hash is already associated with another exchange."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            exchange.lock_tx_hash = tx_hash
            if contract_id:
                exchange.escrow_contract_id = contract_id
            exchange.status = ExchangeStatus.FUNDED
            exchange.save()

        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Confirm physical cash handover",
        description=(
            "Confirms physical cash delivery. Blocked if the exchange is disputed or already settled. "
            "For CASH_OUT, user confirms directly or agent provides the OTP secret. "
            "For CASH_IN, agent confirms directly or user provides the OTP secret."
        ),
        request=ConfirmCashSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'], url_path='confirm-cash')
    def confirm_cash(self, request, pk=None):
        exchange = self.get_object()
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)

        if exchange.status == ExchangeStatus.DISPUTED:
            return Response(
                {"error": "Cannot confirm cash while exchange is under dispute."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if exchange.status not in [ExchangeStatus.FUNDED, ExchangeStatus.CASH_PENDING]:
            return Response(
                {"error": f"Cannot confirm cash for exchange in status {exchange.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ConfirmCashSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        secret = serializer.validated_data.get('secret', '').strip()

        is_depositor = (stellar_address == exchange.depositor_address)
        is_recipient = (stellar_address == exchange.recipient_address)

        if is_recipient:
            if not secret or secret != exchange.secret:
                return Response(
                    {"error": "Invalid handover secret / OTP."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif not is_depositor:
            return Response({"error": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)

        exchange.status = ExchangeStatus.CASH_CONFIRMED
        exchange.save(update_fields=['status', 'updated_at'])

        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Notify that USDC funds are released from escrow",
        description=(
            "Records the on-chain release transaction hash after settlement. Enforces duplicate "
            "settlement protection, blocks disputed exchanges, settles committed agent liquidity, "
            "and increments completed trades."
        ),
        request=NotifyReleasedSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'], url_path='notify-released')
    def notify_released(self, request, pk=None):
        exchange = self.get_object()
        serializer = NotifyReleasedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tx_hash = serializer.validated_data['tx_hash']

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)

            if exchange.status == ExchangeStatus.DISPUTED:
                return Response(
                    {"error": "Disputed transactions cannot settle through normal release."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if exchange.status in SETTLED_STATUSES:
                return Response(
                    {"error": f"Duplicate settlement rejected: exchange is already {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if exchange.status not in [
                ExchangeStatus.FUNDED,
                ExchangeStatus.CASH_PENDING,
                ExchangeStatus.CASH_CONFIRMED,
            ]:
                return Response(
                    {"error": f"Cannot release exchange in status {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if Exchange.objects.filter(
                Q(release_tx_hash=tx_hash) | Q(resolution_tx_hash=tx_hash)
            ).exclude(id=exchange.id).exists():
                return Response(
                    {"error": "Duplicate settlement transaction hash already recorded on another exchange."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            exchange.release_tx_hash = tx_hash
            exchange.status = ExchangeStatus.RELEASED
            exchange.settled_at = timezone.now()
            _settle_committed_liquidity(
                exchange,
                note=f"Settled liquidity for completed Exchange #{exchange.exchange_id}",
            )
            exchange.save()

            AgentProfile.objects.filter(id=exchange.agent_id).update(
                completed_trades=F('completed_trades') + 1
            )

        invalidate_agents_cache()
        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Cancel or refund an exchange",
        description=(
            "Allows eligible cancellation before settlement or refund after escrow timeout. "
            "Blocked for disputed or already-settled exchanges. Restores committed agent liquidity."
        ),
        request=CancelExchangeSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        exchange = self.get_object()
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)

        serializer = CancelExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tx_hash = serializer.validated_data.get('tx_hash', '').strip()
        reason = serializer.validated_data.get('reason', '').strip()

        is_user = (exchange.user_address == stellar_address)
        is_agent = (exchange.agent.stellar_address == stellar_address)
        if not (is_user or is_agent):
            return Response({"error": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)

            if exchange.status == ExchangeStatus.DISPUTED:
                return Response(
                    {"error": "Disputed transactions cannot be cancelled through normal flow."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if exchange.status in SETTLED_STATUSES:
                return Response(
                    {"error": f"Exchange is already resolved with status {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = int(time.time())
            is_depositor = (stellar_address == exchange.depositor_address)
            if is_depositor and exchange.status == ExchangeStatus.FUNDED and now < exchange.timeout_timestamp:
                return Response(
                    {"error": f"Escrow timeout not yet reached. Refund allowed after {exchange.timeout_timestamp}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if tx_hash and Exchange.objects.filter(refund_tx_hash=tx_hash).exclude(id=exchange.id).exists():
                return Response(
                    {"error": "This refund transaction hash is already recorded on another exchange."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if reason:
                exchange.cancellation_reason = reason

            if exchange.status in [ExchangeStatus.FUNDED, ExchangeStatus.CASH_PENDING, ExchangeStatus.CASH_CONFIRMED]:
                exchange.status = ExchangeStatus.REFUNDED
                if tx_hash:
                    exchange.refund_tx_hash = tx_hash
            else:
                exchange.status = ExchangeStatus.CANCELLED

            exchange.settled_at = timezone.now()
            _release_committed_liquidity(
                exchange,
                note=f"Restored liquidity after {exchange.status} on Exchange #{exchange.exchange_id}",
            )
            exchange.save()

        invalidate_agents_cache()
        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Report a dispute on an exchange",
        description=(
            "Allows either party (user or agent) to raise a dispute with a reason, optional "
            "supporting evidence references, and optional on-chain dispute transaction hash. "
            "Transitions status to DISPUTED and freezes normal settlement."
        ),
        request=DisputeExchangeSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'])
    def dispute(self, request, pk=None):
        exchange = self.get_object()
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)

        is_user = (exchange.user_address == stellar_address)
        is_agent = (exchange.agent.stellar_address == stellar_address)
        if not (is_user or is_agent):
            return Response({"error": "Unauthorized."}, status=status.HTTP_403_FORBIDDEN)

        serializer = DisputeExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data['reason']
        evidence_references = serializer.validated_data.get('evidence_references', [])
        tx_hash = serializer.validated_data.get('tx_hash', '').strip() or None

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)

            if exchange.status == ExchangeStatus.DISPUTED:
                return Response(
                    {"error": "A dispute is already open for this exchange."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if exchange.status not in [
                ExchangeStatus.FUNDED,
                ExchangeStatus.CASH_PENDING,
                ExchangeStatus.CASH_CONFIRMED,
            ]:
                return Response(
                    {"error": f"Cannot dispute exchange in status {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            Dispute.objects.create(
                exchange=exchange,
                opened_by=stellar_address,
                opened_by_role='USER' if is_user else 'AGENT',
                reason=reason,
                evidence_references=evidence_references,
                status=DisputeStatus.OPEN,
                dispute_tx_hash=tx_hash,
            )

            exchange.status = ExchangeStatus.DISPUTED
            if tx_hash:
                exchange.dispute_tx_hash = tx_hash
            exchange.notes = f"Dispute: {reason}"
            exchange.save(update_fields=['status', 'dispute_tx_hash', 'notes', 'updated_at'])

        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Resolve a disputed exchange (authorized arbitrator only)",
        description=(
            "Executes an approved dispute resolution (RELEASE or REFUND) after on-chain arbitrator "
            "settlement. Enforces arbitrator authorization, prevents duplicate settlement, and updates "
            "agent liquidity accordingly."
        ),
        request=ResolveDisputeSerializer,
        responses={200: ExchangeSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'], url_path='resolve-dispute')
    def resolve_dispute(self, request, pk=None):
        if not is_authorized_arbitrator(request):
            return Response(
                {"error": "Only an authorized dispute arbitrator can resolve disputes."},
                status=status.HTTP_403_FORBIDDEN,
            )

        exchange = self.get_object()
        resolver_address = getattr(request.user, 'stellar_address', request.user.username)

        serializer = ResolveDisputeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data['decision']
        resolution_notes = serializer.validated_data.get('resolution_notes', '')
        tx_hash = serializer.validated_data['tx_hash']

        with transaction.atomic():
            exchange = Exchange.objects.select_for_update().get(id=exchange.id)

            if exchange.status in SETTLED_STATUSES:
                return Response(
                    {"error": f"Duplicate settlement rejected: exchange is already {exchange.status}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if exchange.status != ExchangeStatus.DISPUTED:
                return Response(
                    {"error": f"Exchange is not in DISPUTED status (current: {exchange.status})."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if Exchange.objects.filter(
                Q(resolution_tx_hash=tx_hash) | Q(release_tx_hash=tx_hash) | Q(refund_tx_hash=tx_hash)
            ).exclude(id=exchange.id).exists():
                return Response(
                    {"error": "This resolution transaction hash is already recorded on another exchange."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            now = timezone.now()
            dispute = Dispute.objects.select_for_update().filter(exchange=exchange).first()

            if decision == DisputeDecision.RELEASE:
                exchange.status = ExchangeStatus.RELEASED
                exchange.release_tx_hash = tx_hash
                exchange.resolution_tx_hash = tx_hash
                exchange.settled_at = now
                _settle_committed_liquidity(
                    exchange,
                    note=f"Settled liquidity via dispute RELEASE on Exchange #{exchange.exchange_id}",
                )
                AgentProfile.objects.filter(id=exchange.agent_id).update(
                    completed_trades=F('completed_trades') + 1
                )
                dispute_status = DisputeStatus.RESOLVED_RELEASE
            else:
                exchange.status = ExchangeStatus.REFUNDED
                exchange.refund_tx_hash = tx_hash
                exchange.resolution_tx_hash = tx_hash
                exchange.settled_at = now
                _release_committed_liquidity(
                    exchange,
                    note=f"Restored liquidity via dispute REFUND on Exchange #{exchange.exchange_id}",
                )
                dispute_status = DisputeStatus.RESOLVED_REFUND

            exchange.save()

            if dispute:
                dispute.status = dispute_status
                dispute.resolution_decision = decision
                dispute.resolution_notes = resolution_notes
                dispute.resolved_by = resolver_address
                dispute.resolution_tx_hash = tx_hash
                dispute.resolved_at = now
                dispute.save()

        invalidate_agents_cache()
        return Response(ExchangeSerializer(exchange).data)

    @extend_schema(
        summary="Verify Stellar Testnet transaction hashes for an exchange",
        description=(
            "Queries Stellar Horizon to verify recorded lock, release, refund, dispute, and "
            "resolution transaction hashes and returns verifiable explorer links."
        ),
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['get'])
    def verify(self, request, pk=None):
        exchange = self.get_object()
        report = verify_exchange_transactions(exchange)
        return Response(report)

    @extend_schema(
        summary="Leave a rating and review for an agent",
        description="Allows the customer to submit a 1 to 5 star rating and comment after funds are released. Automatically recalculates agent reputation score.",
        request=ReviewCreateSerializer,
        responses={201: AgentReviewSerializer},
        tags=["Exchanges & Escrow"],
    )
    @action(detail=True, methods=['post'])
    def review(self, request, pk=None):
        exchange = self.get_object()
        stellar_address = getattr(request.user, 'stellar_address', request.user.username)

        if exchange.user_address != stellar_address:
            return Response({"error": "Only the customer can review the agent."}, status=status.HTTP_403_FORBIDDEN)

        if exchange.status != ExchangeStatus.RELEASED:
            return Response({"error": "Can only review completed (RELEASED) exchanges."}, status=status.HTTP_400_BAD_REQUEST)

        if hasattr(exchange, 'review'):
            return Response({"error": "A review has already been submitted for this exchange."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ReviewCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        rev = AgentReview.objects.create(
            exchange=exchange,
            agent=exchange.agent,
            user_address=stellar_address,
            rating=serializer.validated_data['rating'],
            comment=serializer.validated_data.get('comment', ''),
        )

        # Recalculate average reputation score
        avg_rating = exchange.agent.reviews.aggregate(Avg('rating'))['rating__avg']
        if avg_rating is not None:
            exchange.agent.reputation_score = Decimal(str(round(avg_rating, 2)))
            exchange.agent.save(update_fields=['reputation_score'])
            invalidate_agents_cache()

        return Response(AgentReviewSerializer(rev).data, status=status.HTTP_201_CREATED)


class DisputeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DisputeSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        stellar_address = getattr(self.request.user, 'stellar_address', self.request.user.username)
        if is_authorized_arbitrator(self.request):
            queryset = Dispute.objects.all()
        else:
            queryset = Dispute.objects.filter(
                Q(exchange__user_address=stellar_address)
                | Q(exchange__agent__stellar_address=stellar_address)
            )

        queryset = queryset.select_related('exchange', 'exchange__agent').order_by('-created_at')

        status_param = self.request.query_params.get('status', '').strip().upper()
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset


class ConfigView(APIView):
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Retrieve Stellar Testnet network and contract configuration",
        description="Returns Stellar network passphrase, RPC URL, Horizon URL, Explorer URL, USDC Asset Contract ID, and Escrow Contract ID.",
        responses={200: ConfigSerializer},
        tags=["System Configuration"],
    )
    def get(self, request):
        data = {
            "network_passphrase": getattr(settings, 'STELLAR_NETWORK_PASSPHRASE', 'Test SDF Network ; September 2015'),
            "rpc_url": getattr(settings, 'STELLAR_RPC_URL', 'https://soroban-testnet.stellar.org'),
            "horizon_url": getattr(settings, 'STELLAR_HORIZON_URL', 'https://horizon-testnet.stellar.org'),
            "explorer_base_url": getattr(settings, 'STELLAR_EXPLORER_BASE_URL', 'https://stellar.expert/explorer/testnet/tx'),
            "usdc_contract_id": getattr(settings, 'USDC_ASSET_CONTRACT_ID', ''),
            "escrow_contract_id": getattr(settings, 'ESCROW_CONTRACT_ID', ''),
        }
        return Response(data)


class FileUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="Upload image to media storage",
        description="Upload an avatar image, agent storefront picture, or dispute receipt to local media storage (max 5MB, JPG/PNG/WEBP/GIF). Returns the media URL.",
        request=FileUploadRequestSerializer,
        responses={201: FileUploadResponseSerializer, 400: OpenApiResponse(description="Invalid file or size exceeded")},
        tags=["Media Storage"],
    )
    def post(self, request):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file uploaded. Please supply 'file' field."}, status=status.HTTP_400_BAD_REQUEST)

        if file_obj.size > MAX_FILE_SIZE_BYTES:
            return Response({"error": f"File size exceeds 5MB limit ({file_obj.size} bytes)."}, status=status.HTTP_400_BAD_REQUEST)

        content_type = file_obj.content_type
        if content_type not in ALLOWED_IMAGE_TYPES:
            return Response({
                "error": f"Unsupported media type '{content_type}'. Allowed types: {', '.join(ALLOWED_IMAGE_TYPES.keys())}"
            }, status=status.HTTP_400_BAD_REQUEST)

        file_bytes = file_obj.read()
        public_url, object_key = upload_object(file_bytes, file_obj.name, content_type)

        return Response({
            "url": public_url,
            "object_key": object_key,
            "content_type": content_type,
            "size": len(file_bytes),
        }, status=status.HTTP_201_CREATED)

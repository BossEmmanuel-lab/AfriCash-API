from decimal import Decimal
from django.db import models


class UserProfile(models.Model):
    # Non-custodial user profile bound to connected Stellar wallet
    stellar_address = models.CharField(max_length=56, unique=True, db_index=True)
    display_name = models.CharField(max_length=100, blank=True)
    avatar_url = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.display_name or self.stellar_address[:6]}...{self.stellar_address[-4:]}"


class AgentProfile(models.Model):
    # Stellar public address of the cash agent
    stellar_address = models.CharField(max_length=56, unique=True, db_index=True)
    name = models.CharField(max_length=150)
    avatar_url = models.CharField(max_length=500, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address_text = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    supported_currencies = models.JSONField(default=list)
    exchange_rates = models.JSONField(default=dict)
    available_services = models.JSONField(default=list)
    handover_methods = models.JSONField(default=list)
    operating_hours = models.CharField(max_length=100, blank=True, default="08:00 - 20:00")
    fee_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1.00'))
    is_available = models.BooleanField(default=True, db_index=True)
    is_verified = models.BooleanField(default=False, db_index=True)
    reputation_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('5.00'))
    completed_trades = models.PositiveIntegerField(default=0)
    min_usdc_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('5.00'))
    max_usdc_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('1000.00'))
    # v2.0 Agent Liquidity Management
    available_liquidity_usdc = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal('0.000000')
    )
    committed_liquidity_usdc = models.DecimalField(
        max_digits=18, decimal_places=6, default=Decimal('0.000000')
    )
    liquidity_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_liquidity_usdc(self) -> Decimal:
        return self.available_liquidity_usdc + self.committed_liquidity_usdc

    @property
    def effective_max_usdc_amount(self) -> Decimal:
        return min(self.max_usdc_amount, self.available_liquidity_usdc)

    @property
    def supported_transaction_types(self) -> list:
        services = [str(s).lower() for s in (self.available_services or [])]
        types = []
        if 'cash_out' in services:
            types.append('CASH_OUT')
        if 'cash_in' in services:
            types.append('CASH_IN')
        return types

    def __str__(self):
        return f"{self.name} ({self.stellar_address[:6]}...{self.stellar_address[-4:]})"


class ExchangeType(models.TextChoices):
    CASH_OUT = 'CASH_OUT', 'Cash Out (USDC to Cash)'
    CASH_IN = 'CASH_IN', 'Cash In (Cash to USDC)'


class ExchangeStatus(models.TextChoices):
    INITIATED = 'INITIATED', 'Initiated'
    ACCEPTED = 'ACCEPTED', 'Accepted by Agent'
    FUNDED = 'FUNDED', 'Funded (Escrow Locked)'
    CASH_PENDING = 'CASH_PENDING', 'Cash Pending'
    CASH_CONFIRMED = 'CASH_CONFIRMED', 'Cash Confirmed'
    RELEASED = 'RELEASED', 'Released'
    CANCELLED = 'CANCELLED', 'Cancelled'
    REFUNDED = 'REFUNDED', 'Refunded'
    DISPUTED = 'DISPUTED', 'Disputed'


class Exchange(models.Model):
    # Matches Soroban escrow contract exchange_id
    exchange_id = models.BigIntegerField(unique=True, db_index=True)
    transaction_type = models.CharField(
        max_length=16,
        choices=ExchangeType.choices,
        default=ExchangeType.CASH_OUT,
        db_index=True,
    )
    user_address = models.CharField(max_length=56, db_index=True)
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='exchanges')
    usdc_amount = models.DecimalField(max_digits=18, decimal_places=6)
    fiat_currency = models.CharField(max_length=10)
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=4)
    fiat_amount = models.DecimalField(max_digits=18, decimal_places=2)
    secret_hash = models.CharField(max_length=64, blank=True)
    secret = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=20,
        choices=ExchangeStatus.choices,
        default=ExchangeStatus.INITIATED,
        db_index=True,
    )
    escrow_contract_id = models.CharField(max_length=64, blank=True)
    token_address = models.CharField(max_length=64, blank=True)
    lock_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    release_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    refund_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    dispute_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    resolution_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    timeout_timestamp = models.BigIntegerField()
    cancellation_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    # v2.0 lifecycle & liquidity accounting
    liquidity_committed = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def depositor_address(self) -> str:
        if self.transaction_type == ExchangeType.CASH_IN:
            return self.agent.stellar_address
        return self.user_address

    @property
    def recipient_address(self) -> str:
        if self.transaction_type == ExchangeType.CASH_IN:
            return self.user_address
        return self.agent.stellar_address

    def __str__(self):
        return f"Exchange #{self.exchange_id} [{self.transaction_type}] - {self.status} ({self.usdc_amount} USDC)"


class DisputeStatus(models.TextChoices):
    OPEN = 'OPEN', 'Open'
    UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
    RESOLVED_RELEASE = 'RESOLVED_RELEASE', 'Resolved (Released)'
    RESOLVED_REFUND = 'RESOLVED_REFUND', 'Resolved (Refunded)'


class DisputeDecision(models.TextChoices):
    RELEASE = 'RELEASE', 'Release to Recipient'
    REFUND = 'REFUND', 'Refund to Depositor'


class Dispute(models.Model):
    exchange = models.OneToOneField(Exchange, on_delete=models.CASCADE, related_name='dispute')
    opened_by = models.CharField(max_length=56, db_index=True)
    opened_by_role = models.CharField(max_length=16, default='USER')
    reason = models.CharField(max_length=500)
    evidence_references = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=24,
        choices=DisputeStatus.choices,
        default=DisputeStatus.OPEN,
        db_index=True,
    )
    dispute_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    resolution_decision = models.CharField(
        max_length=16,
        choices=DisputeDecision.choices,
        blank=True,
    )
    resolution_notes = models.TextField(blank=True)
    resolved_by = models.CharField(max_length=56, blank=True)
    resolution_tx_hash = models.CharField(max_length=66, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Dispute for Exchange #{self.exchange.exchange_id} - {self.status}"


class LiquidityEventType(models.TextChoices):
    REGISTER = 'REGISTER', 'Liquidity Registered'
    UPDATE = 'UPDATE', 'Liquidity Updated'
    COMMIT = 'COMMIT', 'Liquidity Committed'
    RELEASE_COMMIT = 'RELEASE_COMMIT', 'Commitment Released (Cancel/Refund)'
    SETTLE = 'SETTLE', 'Commitment Settled (Completed)'


class AgentLiquidityLog(models.Model):
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='liquidity_logs')
    exchange = models.ForeignKey(
        Exchange, on_delete=models.SET_NULL, null=True, blank=True, related_name='liquidity_logs'
    )
    event_type = models.CharField(max_length=24, choices=LiquidityEventType.choices, db_index=True)
    amount_delta = models.DecimalField(max_digits=18, decimal_places=6)
    available_after = models.DecimalField(max_digits=18, decimal_places=6)
    committed_after = models.DecimalField(max_digits=18, decimal_places=6)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.agent.name} [{self.event_type}] delta={self.amount_delta}"


class AgentReview(models.Model):
    exchange = models.OneToOneField(Exchange, on_delete=models.CASCADE, related_name='review')
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='reviews')
    user_address = models.CharField(max_length=56)
    rating = models.PositiveSmallIntegerField(default=5)  # 1 to 5 stars
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Review for {self.agent.name}: {self.rating} stars by {self.user_address[:6]}..."

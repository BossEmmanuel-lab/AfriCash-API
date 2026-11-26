from django.contrib import admin
from api.models import (
    AgentProfile,
    Exchange,
    UserProfile,
    AgentReview,
    Dispute,
    AgentLiquidityLog,
)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('stellar_address', 'display_name', 'created_at')
    search_fields = ('stellar_address', 'display_name')


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'stellar_address',
        'city',
        'country',
        'available_liquidity_usdc',
        'committed_liquidity_usdc',
        'is_available',
        'is_verified',
        'reputation_score',
        'completed_trades',
    )
    list_filter = ('is_available', 'is_verified', 'country', 'city')
    search_fields = ('name', 'stellar_address', 'city')


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = (
        'exchange_id',
        'transaction_type',
        'user_address',
        'agent',
        'usdc_amount',
        'fiat_currency',
        'fiat_amount',
        'status',
        'liquidity_committed',
        'created_at',
    )
    list_filter = ('status', 'transaction_type', 'fiat_currency', 'liquidity_committed')
    search_fields = (
        'exchange_id',
        'user_address',
        'lock_tx_hash',
        'release_tx_hash',
        'refund_tx_hash',
        'dispute_tx_hash',
        'resolution_tx_hash',
    )


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = (
        'exchange',
        'opened_by',
        'opened_by_role',
        'status',
        'resolution_decision',
        'resolved_by',
        'created_at',
        'resolved_at',
    )
    list_filter = ('status', 'opened_by_role', 'resolution_decision')
    search_fields = ('exchange__exchange_id', 'opened_by', 'resolved_by', 'reason')


@admin.register(AgentLiquidityLog)
class AgentLiquidityLogAdmin(admin.ModelAdmin):
    list_display = (
        'agent',
        'exchange',
        'event_type',
        'amount_delta',
        'available_after',
        'committed_after',
        'created_at',
    )
    list_filter = ('event_type',)
    search_fields = ('agent__name', 'note')


@admin.register(AgentReview)
class AgentReviewAdmin(admin.ModelAdmin):
    list_display = ('agent', 'user_address', 'rating', 'created_at')
    list_filter = ('rating',)
    search_fields = ('agent__name', 'user_address', 'comment')

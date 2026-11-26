#![no_std]
use soroban_sdk::{
    contract, contracterror, contractevent, contractimpl, contracttype,
    token::TokenClient, Address, Bytes, BytesN, Env,
};

// Storage TTL bounds in ledgers (~5 seconds per ledger)
const BUMP_THRESHOLD: u32 = 30 * 17280;
const BUMP_TO: u32 = 120 * 17280;

#[contracttype]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum ExchangeType {
    /// User deposits USDC into escrow, receives physical cash, agent receives USDC
    CashOut = 1,
    /// Agent deposits USDC into escrow, receives physical cash, user receives USDC
    CashIn = 2,
}

#[contracttype]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum ExchangeStatus {
    Funded = 1,
    Released = 2,
    Refunded = 3,
    Cancelled = 4,
    Disputed = 5,
}

#[contracttype]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum DisputeResolution {
    /// Release locked funds to the exchange recipient (agent for CashOut, user for CashIn)
    ReleaseToRecipient = 1,
    /// Refund locked funds back to the depositor (user for CashOut, agent for CashIn)
    RefundToDepositor = 2,
}

#[contracttype]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Exchange {
    pub exchange_id: u64,
    pub exchange_type: ExchangeType,
    pub user: Address,
    pub agent: Address,
    pub depositor: Address,
    pub recipient: Address,
    pub token: Address,
    pub amount: i128,
    pub secret_hash: BytesN<32>,
    pub timeout_timestamp: u64,
    pub status: ExchangeStatus,
    pub created_at: u64,
    pub disputed_by: Option<Address>,
    pub disputed_at: u64,
    pub settled_at: u64,
}

#[contracttype]
#[derive(Clone)]
pub enum DataKey {
    Admin,
    Arbitrator,
    Exchange(u64),
}

#[contracterror]
#[derive(Copy, Clone, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum EscrowError {
    NotInitialized = 1,
    ExchangeAlreadyExists = 2,
    ExchangeNotFound = 3,
    InvalidAmount = 4,
    InvalidStatus = 5,
    InvalidSecret = 6,
    TimeoutNotReached = 7,
    InvalidTimeout = 8,
    AlreadySettled = 9,
    ExchangeDisputed = 10,
    NotDisputed = 11,
    Unauthorized = 12,
    SameParty = 13,
}

#[contractevent]
pub struct ExchangeFunded {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub depositor: Address,
    #[topic]
    pub recipient: Address,
    pub exchange_type: ExchangeType,
    pub amount: i128,
}

#[contractevent]
pub struct ExchangeReleased {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub recipient: Address,
    pub amount: i128,
}

#[contractevent]
pub struct ExchangeRefunded {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub depositor: Address,
    pub amount: i128,
}

#[contractevent]
pub struct ExchangeCancelled {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub cancelled_by: Address,
    pub amount: i128,
}

#[contractevent]
pub struct ExchangeDisputed {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub disputed_by: Address,
    pub amount: i128,
}

#[contractevent]
pub struct DisputeResolved {
    #[topic]
    pub exchange_id: u64,
    #[topic]
    pub arbitrator: Address,
    pub resolution: DisputeResolution,
    pub recipient: Address,
    pub amount: i128,
}

#[contractevent]
pub struct ArbitratorUpdated {
    #[topic]
    pub admin: Address,
    #[topic]
    pub arbitrator: Address,
}

#[contract]
pub struct AfricCashEscrow;

impl AfricCashEscrow {
    fn is_settled(status: ExchangeStatus) -> bool {
        matches!(
            status,
            ExchangeStatus::Released | ExchangeStatus::Refunded | ExchangeStatus::Cancelled
        )
    }

    fn require_funded_not_disputed(status: ExchangeStatus) -> Result<(), EscrowError> {
        if Self::is_settled(status) {
            return Err(EscrowError::AlreadySettled);
        }
        if status == ExchangeStatus::Disputed {
            return Err(EscrowError::ExchangeDisputed);
        }
        if status != ExchangeStatus::Funded {
            return Err(EscrowError::InvalidStatus);
        }
        Ok(())
    }

    fn bump_ttls(env: &Env, key: &DataKey) {
        env.storage()
            .persistent()
            .extend_ttl(key, BUMP_THRESHOLD, BUMP_TO);
        env.storage().instance().extend_ttl(BUMP_THRESHOLD, BUMP_TO);
    }

    fn do_release(env: &Env, key: &DataKey, exchange: &mut Exchange) {
        exchange.status = ExchangeStatus::Released;
        exchange.settled_at = env.ledger().timestamp();
        env.storage().persistent().set(key, exchange);
        Self::bump_ttls(env, key);

        let contract_address = env.current_contract_address();
        let token_client = TokenClient::new(env, &exchange.token);
        token_client.transfer(&contract_address, &exchange.recipient, &exchange.amount);

        ExchangeReleased {
            exchange_id: exchange.exchange_id,
            recipient: exchange.recipient.clone(),
            amount: exchange.amount,
        }
        .publish(env);
    }
}

#[contractimpl]
impl AfricCashEscrow {
    /// Constructor initializes admin and default dispute arbitrator address.
    pub fn __constructor(env: Env, admin: Address) {
        env.storage().instance().set(&DataKey::Admin, &admin);
        env.storage().instance().set(&DataKey::Arbitrator, &admin);
        env.storage().instance().extend_ttl(BUMP_THRESHOLD, BUMP_TO);
    }

    /// Return configured contract admin.
    pub fn get_admin(env: Env) -> Result<Address, EscrowError> {
        env.storage()
            .instance()
            .get(&DataKey::Admin)
            .ok_or(EscrowError::NotInitialized)
    }

    /// Return configured dispute arbitrator (defaults to admin).
    pub fn get_arbitrator(env: Env) -> Result<Address, EscrowError> {
        env.storage()
            .instance()
            .get(&DataKey::Arbitrator)
            .ok_or(EscrowError::NotInitialized)
    }

    /// Update the authorized dispute arbitrator (admin only).
    pub fn set_arbitrator(env: Env, new_arbitrator: Address) -> Result<(), EscrowError> {
        let admin: Address = env
            .storage()
            .instance()
            .get(&DataKey::Admin)
            .ok_or(EscrowError::NotInitialized)?;
        admin.require_auth();

        env.storage()
            .instance()
            .set(&DataKey::Arbitrator, &new_arbitrator);
        env.storage().instance().extend_ttl(BUMP_THRESHOLD, BUMP_TO);

        ArbitratorUpdated {
            admin,
            arbitrator: new_arbitrator,
        }
        .publish(&env);

        Ok(())
    }

    /// Lock USDC in escrow for a CashOut (user -> agent) or CashIn (agent -> user) exchange.
    pub fn lock_funds(
        env: Env,
        exchange_id: u64,
        exchange_type: ExchangeType,
        user: Address,
        agent: Address,
        token: Address,
        amount: i128,
        secret_hash: BytesN<32>,
        timeout_timestamp: u64,
    ) -> Result<(), EscrowError> {
        if amount <= 0 {
            return Err(EscrowError::InvalidAmount);
        }

        if user == agent {
            return Err(EscrowError::SameParty);
        }

        let now = env.ledger().timestamp();
        if timeout_timestamp <= now {
            return Err(EscrowError::InvalidTimeout);
        }

        let key = DataKey::Exchange(exchange_id);
        if env.storage().persistent().has(&key) {
            return Err(EscrowError::ExchangeAlreadyExists);
        }

        let (depositor, recipient) = match exchange_type {
            ExchangeType::CashOut => (user.clone(), agent.clone()),
            ExchangeType::CashIn => (agent.clone(), user.clone()),
        };

        // Require authorization from the party depositing funds into escrow
        depositor.require_auth();

        // Transfer tokens from depositor into escrow contract
        let contract_address = env.current_contract_address();
        let token_client = TokenClient::new(&env, &token);
        token_client.transfer(&depositor, &contract_address, &amount);

        let exchange = Exchange {
            exchange_id,
            exchange_type,
            user,
            agent,
            depositor: depositor.clone(),
            recipient: recipient.clone(),
            token,
            amount,
            secret_hash,
            timeout_timestamp,
            status: ExchangeStatus::Funded,
            created_at: now,
            disputed_by: None,
            disputed_at: 0,
            settled_at: 0,
        };

        env.storage().persistent().set(&key, &exchange);
        Self::bump_ttls(&env, &key);

        ExchangeFunded {
            exchange_id,
            depositor,
            recipient,
            exchange_type,
            amount,
        }
        .publish(&env);

        Ok(())
    }

    /// Depositor directly confirms cash handover and releases locked funds to recipient.
    /// Blocked if exchange is already settled or currently under dispute.
    pub fn release_by_depositor(env: Env, exchange_id: u64) -> Result<(), EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        Self::require_funded_not_disputed(exchange.status)?;

        exchange.depositor.require_auth();
        Self::do_release(&env, &key, &mut exchange);
        Ok(())
    }

    /// Alias for release_by_depositor preserved for v1.0 CashOut compatibility.
    pub fn release_by_user(env: Env, exchange_id: u64) -> Result<(), EscrowError> {
        Self::release_by_depositor(env, exchange_id)
    }

    /// Recipient releases funds by providing the OTP secret whose SHA-256 matches secret_hash.
    /// Blocked if exchange is already settled or currently under dispute.
    pub fn release_with_secret(
        env: Env,
        exchange_id: u64,
        secret: Bytes,
    ) -> Result<(), EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        Self::require_funded_not_disputed(exchange.status)?;

        // Require authorization from the recipient claiming payment
        exchange.recipient.require_auth();

        // Verify SHA-256 hash of provided secret
        let computed_hash: BytesN<32> = env.crypto().sha256(&secret).into();
        if computed_hash != exchange.secret_hash {
            return Err(EscrowError::InvalidSecret);
        }

        Self::do_release(&env, &key, &mut exchange);
        Ok(())
    }

    /// Depositor claims refund if timeout has elapsed without completion or dispute.
    pub fn refund(env: Env, exchange_id: u64) -> Result<(), EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        Self::require_funded_not_disputed(exchange.status)?;

        // Require depositor authorization
        exchange.depositor.require_auth();

        // Enforce timeout
        let now = env.ledger().timestamp();
        if now < exchange.timeout_timestamp {
            return Err(EscrowError::TimeoutNotReached);
        }

        exchange.status = ExchangeStatus::Refunded;
        exchange.settled_at = now;
        env.storage().persistent().set(&key, &exchange);
        Self::bump_ttls(&env, &key);

        let contract_address = env.current_contract_address();
        let token_client = TokenClient::new(&env, &exchange.token);
        token_client.transfer(&contract_address, &exchange.depositor, &exchange.amount);

        ExchangeRefunded {
            exchange_id,
            depositor: exchange.depositor.clone(),
            amount: exchange.amount,
        }
        .publish(&env);

        Ok(())
    }

    /// Recipient (agent for CashOut, user for CashIn) cancels an exchange immediately
    /// and returns locked funds to the depositor. Blocked if settled or disputed.
    pub fn cancel_by_counterparty(env: Env, exchange_id: u64) -> Result<(), EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        Self::require_funded_not_disputed(exchange.status)?;

        // Require recipient authorization to voluntarily cancel and return funds
        exchange.recipient.require_auth();

        exchange.status = ExchangeStatus::Cancelled;
        exchange.settled_at = env.ledger().timestamp();
        env.storage().persistent().set(&key, &exchange);
        Self::bump_ttls(&env, &key);

        let contract_address = env.current_contract_address();
        let token_client = TokenClient::new(&env, &exchange.token);
        token_client.transfer(&contract_address, &exchange.depositor, &exchange.amount);

        ExchangeCancelled {
            exchange_id,
            cancelled_by: exchange.recipient.clone(),
            amount: exchange.amount,
        }
        .publish(&env);

        Ok(())
    }

    /// Alias for cancel_by_counterparty preserved for v1.0 CashOut compatibility.
    pub fn cancel_by_agent(env: Env, exchange_id: u64) -> Result<(), EscrowError> {
        Self::cancel_by_counterparty(env, exchange_id)
    }

    /// Either party (user or agent) raises a dispute on a funded exchange.
    /// Freezes funds so normal release, refund, and cancel are blocked until arbitrator resolution.
    pub fn raise_dispute(
        env: Env,
        exchange_id: u64,
        caller: Address,
    ) -> Result<(), EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        Self::require_funded_not_disputed(exchange.status)?;

        if caller != exchange.user && caller != exchange.agent {
            return Err(EscrowError::Unauthorized);
        }

        caller.require_auth();

        exchange.status = ExchangeStatus::Disputed;
        exchange.disputed_by = Some(caller.clone());
        exchange.disputed_at = env.ledger().timestamp();
        env.storage().persistent().set(&key, &exchange);
        Self::bump_ttls(&env, &key);

        ExchangeDisputed {
            exchange_id,
            disputed_by: caller,
            amount: exchange.amount,
        }
        .publish(&env);

        Ok(())
    }

    /// Authorized arbitrator resolves a disputed exchange by either releasing funds
    /// to the recipient or refunding them to the depositor. Prevents duplicate settlement.
    pub fn resolve_dispute(
        env: Env,
        exchange_id: u64,
        resolution: DisputeResolution,
    ) -> Result<(), EscrowError> {
        let arbitrator: Address = env
            .storage()
            .instance()
            .get(&DataKey::Arbitrator)
            .ok_or(EscrowError::NotInitialized)?;
        arbitrator.require_auth();

        let key = DataKey::Exchange(exchange_id);
        let mut exchange: Exchange = env
            .storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)?;

        if Self::is_settled(exchange.status) {
            return Err(EscrowError::AlreadySettled);
        }
        if exchange.status != ExchangeStatus::Disputed {
            return Err(EscrowError::NotDisputed);
        }

        let now = env.ledger().timestamp();
        let contract_address = env.current_contract_address();
        let token_client = TokenClient::new(&env, &exchange.token);

        let payout_target = match resolution {
            DisputeResolution::ReleaseToRecipient => {
                exchange.status = ExchangeStatus::Released;
                exchange.settled_at = now;
                env.storage().persistent().set(&key, &exchange);
                Self::bump_ttls(&env, &key);

                token_client.transfer(&contract_address, &exchange.recipient, &exchange.amount);

                ExchangeReleased {
                    exchange_id,
                    recipient: exchange.recipient.clone(),
                    amount: exchange.amount,
                }
                .publish(&env);

                exchange.recipient.clone()
            }
            DisputeResolution::RefundToDepositor => {
                exchange.status = ExchangeStatus::Refunded;
                exchange.settled_at = now;
                env.storage().persistent().set(&key, &exchange);
                Self::bump_ttls(&env, &key);

                token_client.transfer(&contract_address, &exchange.depositor, &exchange.amount);

                ExchangeRefunded {
                    exchange_id,
                    depositor: exchange.depositor.clone(),
                    amount: exchange.amount,
                }
                .publish(&env);

                exchange.depositor.clone()
            }
        };

        DisputeResolved {
            exchange_id,
            arbitrator,
            resolution,
            recipient: payout_target,
            amount: exchange.amount,
        }
        .publish(&env);

        Ok(())
    }

    /// Retrieve exchange state by ID.
    pub fn get_exchange(env: Env, exchange_id: u64) -> Result<Exchange, EscrowError> {
        let key = DataKey::Exchange(exchange_id);
        env.storage()
            .persistent()
            .get(&key)
            .ok_or(EscrowError::ExchangeNotFound)
    }
}

#[cfg(test)]
mod test;

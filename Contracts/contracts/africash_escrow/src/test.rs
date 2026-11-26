#![cfg(test)]
use super::*;
use soroban_sdk::{
    testutils::{Address as _, Ledger},
    token::{StellarAssetClient, TokenClient},
    Address, Bytes, BytesN, Env,
};

fn setup_test(env: &Env) -> (Address, Address, Address, Address, AfricCashEscrowClient<'static>) {
    env.mock_all_auths();

    let admin = Address::generate(env);
    let user = Address::generate(env);
    let agent = Address::generate(env);

    let token_admin = Address::generate(env);
    let token_contract = env.register_stellar_asset_contract_v2(token_admin.clone());
    let token_address = token_contract.address();

    // Mint initial simulated USDC to both user (for CashOut) and agent (for CashIn)
    let token_admin_client = StellarAssetClient::new(env, &token_address);
    token_admin_client.mint(&user, &1000);
    token_admin_client.mint(&agent, &1000);

    let contract_id = env.register(AfricCashEscrow, (admin.clone(),));
    let escrow_client = AfricCashEscrowClient::new(env, &contract_id);

    (admin, user, agent, token_address, escrow_client)
}

#[test]
fn test_cash_out_lock_and_release_by_user() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"OTP1234");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 101u64;
    let amount = 250i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    assert_eq!(token_client.balance(&user), 750);
    assert_eq!(token_client.balance(&client.address), 250);

    let exchange = client.get_exchange(&exchange_id);
    assert_eq!(exchange.status, ExchangeStatus::Funded);
    assert_eq!(exchange.exchange_type, ExchangeType::CashOut);
    assert_eq!(exchange.depositor, user);
    assert_eq!(exchange.recipient, agent);
    assert_eq!(exchange.amount, amount);

    // User confirms cash receipt and releases funds to agent
    client.release_by_user(&exchange_id);

    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(token_client.balance(&agent), 1250);

    let updated = client.get_exchange(&exchange_id);
    assert_eq!(updated.status, ExchangeStatus::Released);
}

#[test]
fn test_cash_in_lock_and_release_by_agent() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"CASHIN-5678");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 102u64;
    let amount = 300i128;
    let timeout = env.ledger().timestamp() + 3600;

    // For CashIn, agent deposits USDC and user receives it
    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashIn,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    assert_eq!(token_client.balance(&agent), 700);
    assert_eq!(token_client.balance(&client.address), 300);

    let exchange = client.get_exchange(&exchange_id);
    assert_eq!(exchange.depositor, agent);
    assert_eq!(exchange.recipient, user);

    // Agent confirms physical cash receipt and releases USDC to user
    client.release_by_depositor(&exchange_id);

    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(token_client.balance(&user), 1300);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Released);
}

#[test]
fn test_lock_and_release_with_secret() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"AFRI-7890");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 103u64;
    let amount = 300i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    client.release_with_secret(&exchange_id, &secret);

    assert_eq!(token_client.balance(&agent), 1300);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Released);
}

#[test]
fn test_release_with_invalid_secret_fails() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);

    let secret = Bytes::from_slice(&env, b"SECRET-123");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 104u64;
    let amount = 100i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    let wrong_secret = Bytes::from_slice(&env, b"WRONG-SECRET");
    let res = client.try_release_with_secret(&exchange_id, &wrong_secret);
    assert_eq!(res, Err(Ok(EscrowError::InvalidSecret)));
}

#[test]
fn test_refund_after_timeout() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"OTP-REFUND");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 105u64;
    let amount = 400i128;
    let timeout = env.ledger().timestamp() + 1800;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    // Attempt refund before timeout should fail
    let early_refund = client.try_refund(&exchange_id);
    assert_eq!(early_refund, Err(Ok(EscrowError::TimeoutNotReached)));

    // Advance ledger timestamp beyond timeout
    env.ledger().set_timestamp(timeout + 10);

    client.refund(&exchange_id);

    assert_eq!(token_client.balance(&user), 1000);
    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Refunded);
}

#[test]
fn test_cancel_by_agent() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"OTP-CANCEL");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 106u64;
    let amount = 150i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    client.cancel_by_agent(&exchange_id);

    assert_eq!(token_client.balance(&user), 1000);
    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Cancelled);
}

#[test]
fn test_duplicate_exchange_id_fails() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);

    let secret = Bytes::from_slice(&env, b"OTP-DUP");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 107u64;
    let amount = 50i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    let res = client.try_lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );
    assert_eq!(res, Err(Ok(EscrowError::ExchangeAlreadyExists)));
}

#[test]
fn test_invalid_amount_timeout_and_same_party() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);

    let secret = Bytes::from_slice(&env, b"OTP-INVALID");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 108u64;
    let timeout = env.ledger().timestamp() + 3600;

    // Zero amount fails
    let res_zero = client.try_lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &0,
        &secret_hash,
        &timeout,
    );
    assert_eq!(res_zero, Err(Ok(EscrowError::InvalidAmount)));

    // Same user and agent fails
    let res_same = client.try_lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &user,
        &token_address,
        &100,
        &secret_hash,
        &timeout,
    );
    assert_eq!(res_same, Err(Ok(EscrowError::SameParty)));

    // Past timeout fails
    env.ledger().set_timestamp(100);
    let res_timeout = client.try_lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &100,
        &secret_hash,
        &50,
    );
    assert_eq!(res_timeout, Err(Ok(EscrowError::InvalidTimeout)));
}

#[test]
fn test_duplicate_settlement_rejected() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);

    let secret = Bytes::from_slice(&env, b"OTP-ONCE");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 201u64;
    let amount = 200i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    client.release_by_user(&exchange_id);

    // Second release, secret release, refund, cancel, and dispute must all be rejected
    assert_eq!(
        client.try_release_by_user(&exchange_id),
        Err(Ok(EscrowError::AlreadySettled))
    );
    assert_eq!(
        client.try_release_with_secret(&exchange_id, &secret),
        Err(Ok(EscrowError::AlreadySettled))
    );
    env.ledger().set_timestamp(timeout + 10);
    assert_eq!(
        client.try_refund(&exchange_id),
        Err(Ok(EscrowError::AlreadySettled))
    );
    assert_eq!(
        client.try_cancel_by_agent(&exchange_id),
        Err(Ok(EscrowError::AlreadySettled))
    );
    assert_eq!(
        client.try_raise_dispute(&exchange_id, &user),
        Err(Ok(EscrowError::AlreadySettled))
    );
}

#[test]
fn test_dispute_blocks_normal_settlement_and_resolves_to_release() {
    let env = Env::default();
    let (admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let secret = Bytes::from_slice(&env, b"OTP-DISPUTE-1");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 301u64;
    let amount = 350i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    // Third-party outsider cannot raise a dispute
    let outsider = Address::generate(&env);
    assert_eq!(
        client.try_raise_dispute(&exchange_id, &outsider),
        Err(Ok(EscrowError::Unauthorized))
    );

    // User raises dispute
    client.raise_dispute(&exchange_id, &user);
    let disputed = client.get_exchange(&exchange_id);
    assert_eq!(disputed.status, ExchangeStatus::Disputed);
    assert_eq!(disputed.disputed_by, Some(user.clone()));

    // Normal release, secret release, cancel, and timeout refund are all blocked while disputed
    assert_eq!(
        client.try_release_by_user(&exchange_id),
        Err(Ok(EscrowError::ExchangeDisputed))
    );
    assert_eq!(
        client.try_release_with_secret(&exchange_id, &secret),
        Err(Ok(EscrowError::ExchangeDisputed))
    );
    assert_eq!(
        client.try_cancel_by_agent(&exchange_id),
        Err(Ok(EscrowError::ExchangeDisputed))
    );
    env.ledger().set_timestamp(timeout + 100);
    assert_eq!(
        client.try_refund(&exchange_id),
        Err(Ok(EscrowError::ExchangeDisputed))
    );

    // Arbitrator (defaults to admin) resolves in favor of recipient (agent)
    assert_eq!(client.get_arbitrator(), admin);
    client.resolve_dispute(&exchange_id, &DisputeResolution::ReleaseToRecipient);

    assert_eq!(token_client.balance(&agent), 1350);
    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Released);

    // Duplicate resolution is rejected
    assert_eq!(
        client.try_resolve_dispute(&exchange_id, &DisputeResolution::RefundToDepositor),
        Err(Ok(EscrowError::AlreadySettled))
    );
}

#[test]
fn test_dispute_resolves_to_refund_with_custom_arbitrator() {
    let env = Env::default();
    let (_admin, user, agent, token_address, client) = setup_test(&env);
    let token_client = TokenClient::new(&env, &token_address);

    let custom_arbitrator = Address::generate(&env);
    client.set_arbitrator(&custom_arbitrator);
    assert_eq!(client.get_arbitrator(), custom_arbitrator);

    let secret = Bytes::from_slice(&env, b"OTP-DISPUTE-2");
    let secret_hash: BytesN<32> = env.crypto().sha256(&secret).into();

    let exchange_id = 302u64;
    let amount = 225i128;
    let timeout = env.ledger().timestamp() + 3600;

    client.lock_funds(
        &exchange_id,
        &ExchangeType::CashOut,
        &user,
        &agent,
        &token_address,
        &amount,
        &secret_hash,
        &timeout,
    );

    // Cannot resolve an exchange that is not disputed
    assert_eq!(
        client.try_resolve_dispute(&exchange_id, &DisputeResolution::RefundToDepositor),
        Err(Ok(EscrowError::NotDisputed))
    );

    // Agent raises dispute
    client.raise_dispute(&exchange_id, &agent);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Disputed);

    // Arbitrator refunds depositor (user)
    client.resolve_dispute(&exchange_id, &DisputeResolution::RefundToDepositor);

    assert_eq!(token_client.balance(&user), 1000);
    assert_eq!(token_client.balance(&client.address), 0);
    assert_eq!(client.get_exchange(&exchange_id).status, ExchangeStatus::Refunded);
}

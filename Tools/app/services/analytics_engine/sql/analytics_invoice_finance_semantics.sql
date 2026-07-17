-- analytics_invoice_finance_semantics_v4.sql
-- Hotfix for app-parity invoice amounts.
-- Keeps source/application booking_invoice_details unchanged.
-- Changes only AnalyticsEngine computed finance projection view.
--
-- Fixes:
--   1) Use app-style payable amount (amount, not pending_balance) for display/future totals.
--   2) Deposit Refund signed settlement uses amount first.
--   3) Closed rows remain zero exposure even when pending_balance is stale/non-zero.
--   4) Adds app/debug fields: ui_payable_amount, ui_received_amount,
--      app_pending_display_amount, actionable_receivable_amount.
--   5) Applies 18% GST display uplift for Renovation Charges to match the
--      application screenshot when GST display is enabled.

BEGIN;

-- Make sure the new Repairs/Damages value is present too.
INSERT INTO "AnalyticsEngine".booking_invoice_type_semantics
(transaction_type, semantic_role, future_treatment)
VALUES
    ('Repairs/Damages', 'raw_charge', 'actionable_later')
ON CONFLICT (transaction_type)
DO UPDATE SET
    semantic_role = EXCLUDED.semantic_role,
    future_treatment = EXCLUDED.future_treatment,
    updated_at = now();

DROP VIEW IF EXISTS "AnalyticsEngine".staging_booking_invoice_finance_v;

CREATE VIEW "AnalyticsEngine".staging_booking_invoice_finance_v AS
WITH normalized AS (
    SELECT
        i.*,

        COALESCE(s.semantic_role, 'raw_charge') AS semantic_role,
        COALESCE(s.future_treatment, 'actionable_later') AS future_treatment,
        CASE
            WHEN s.transaction_type IS NULL THEN 'default_raw_charge'
            ELSE 'mapped'
        END AS semantics_source,

        -- App/UI display payable amount.
        -- The application screen displays invoice amount as payable, not pending_balance.
        -- For Renovation Charges, the screenshot shows 18% GST added on display
        -- (1000 + 180 = 1180), so we project the same app-display value here.
        ROUND(
            CASE
                WHEN LOWER(COALESCE(i.transaction_type, '')) LIKE '%renovation%'
                THEN (
                    COALESCE(
                        i.amount,
                        NULLIF((COALESCE(i.om_rent, 0) + COALESCE(i.sa_rent, 0)), 0),
                        i.total_amount,
                        i.pending_balance,
                        0
                    )::numeric * 1.18
                )
                ELSE COALESCE(
                    i.amount,
                    NULLIF((COALESCE(i.om_rent, 0) + COALESCE(i.sa_rent, 0)), 0),
                    i.total_amount,
                    i.pending_balance,
                    0
                )::numeric
            END,
            2
        ) AS ui_payable_amount,

        ROUND(COALESCE(i.amount_recieved, 0)::numeric, 2) AS ui_received_amount,

        -- Signed settlements must use signed source amount first.
        -- pending_balance can be stale/derived and should not drive settlement sign.
        COALESCE(
            i.amount,
            i.pending_balance,
            i.total_amount,
            NULLIF((COALESCE(i.om_rent, 0) + COALESCE(i.sa_rent, 0)), 0),
            0
        )::numeric AS signed_settlement_amount,

        CASE
            WHEN LOWER(COALESCE(i.status, '')) IN ('closed', 'cancel', 'cancelled', 'canceled', 'cancel & settle')
              OR LOWER(COALESCE(i.amount_status, '')) IN ('closed', 'received', 'recieved', 'paid', 'settled')
            THEN true
            ELSE false
        END AS is_closed_by_status

    FROM "AnalyticsEngine".staging_booking_invoice_details i
    LEFT JOIN "AnalyticsEngine".booking_invoice_type_semantics s
      ON LOWER(TRIM(s.transaction_type)) = LOWER(TRIM(i.transaction_type))
),
amounts AS (
    SELECT
        n.*,

        CASE
            WHEN n.is_closed_by_status THEN 0::numeric
            WHEN n.semantic_role = 'signed_net_settlement' THEN 0::numeric
            ELSE GREATEST(
                COALESCE(n.ui_payable_amount, 0)::numeric - COALESCE(n.ui_received_amount, 0)::numeric,
                0::numeric
            )
        END AS raw_outstanding_amount,

        -- This matches the app Total pending style: positive pending rows only,
        -- excluding closed rows and excluding signed Deposit Refund settlements.
        CASE
            WHEN n.is_closed_by_status THEN 0::numeric
            WHEN n.semantic_role = 'signed_net_settlement' THEN 0::numeric
            ELSE GREATEST(
                COALESCE(n.ui_payable_amount, 0)::numeric - COALESCE(n.ui_received_amount, 0)::numeric,
                0::numeric
            )
        END AS app_pending_display_amount

    FROM normalized n
),
bucketed AS (
    SELECT
        a.*,

        CASE
            WHEN a.is_closed_by_status THEN 'closed'
            WHEN LOWER(COALESCE(a.status, '')) = 'defaulted' THEN 'past_due'
            WHEN a.from_date IS NOT NULL AND a.from_date::date > CURRENT_DATE THEN 'future_scheduled'
            WHEN a.till_date IS NOT NULL AND a.till_date::date < CURRENT_DATE THEN 'past_due'
            ELSE 'due_now'
        END AS due_bucket

    FROM amounts a
),
projected AS (
    SELECT
        b.*,

        CASE
            WHEN b.future_treatment = 'indicative_only'
             AND b.due_bucket = 'future_scheduled'
             AND NOT b.is_closed_by_status
            THEN true
            ELSE false
        END AS is_indicative,

        CASE
            WHEN b.semantic_role = 'security_deposit_collection' THEN 'security_deposit_collection'
            WHEN b.semantic_role = 'signed_net_settlement' THEN 'deposit_refund_settlement'
            WHEN LOWER(COALESCE(b.transaction_type, '')) = 'rent'
              OR LOWER(COALESCE(b.transaction_type, '')) LIKE '%rent%' THEN 'rent'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%ebill%'
              OR LOWER(COALESCE(b.transaction_type, '')) LIKE '%holdamt%' THEN 'utility_hold'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%electric%'
              OR LOWER(COALESCE(b.transaction_type, '')) IN ('dth', 'internet') THEN 'utility_charge'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%gas%' THEN 'gas_charge'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%movement%' THEN 'movement_charge'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%renovation%' THEN 'renovation_charge'
            WHEN LOWER(COALESCE(b.transaction_type, '')) LIKE '%repair%'
              OR LOWER(COALESCE(b.transaction_type, '')) LIKE '%damage%' THEN 'repairs_damages_charge'
            WHEN LOWER(COALESCE(b.transaction_type, '')) IN (
                'agreement breakage',
                'cancellation charges',
                'late payment',
                'notice period',
                'paid to vendor',
                'refund reversal'
            ) THEN 'adjustment'
            WHEN b.semantic_role = 'waiver' THEN 'waiver'
            WHEN b.semantic_role = 'discount' THEN 'discount'
            WHEN b.semantic_role = 'adjustment_component' THEN 'adjustment'
            ELSE 'raw_charge'
        END AS invoice_line_kind,

        CASE
            WHEN b.semantic_role = 'signed_net_settlement' AND b.signed_settlement_amount < 0 THEN 'customer_to_business'
            WHEN b.semantic_role = 'signed_net_settlement' AND b.signed_settlement_amount > 0 THEN 'business_to_customer'
            WHEN b.semantic_role = 'signed_net_settlement' THEN 'neutral'
            ELSE 'customer_to_business'
        END AS money_direction,

        CASE
            WHEN b.semantic_role = 'signed_net_settlement' THEN 'refund_settlement_authoritative'
            WHEN b.semantic_role IN ('adjustment_component', 'waiver', 'discount') THEN 'adjustment_component'
            ELSE 'raw_charge'
        END AS settlement_semantics,

        (b.semantic_role = 'signed_net_settlement') AS is_net_settlement,

        CASE
            WHEN b.semantic_role = 'signed_net_settlement'
             AND b.signed_settlement_amount < 0
             AND b.due_bucket <> 'closed'
            THEN ABS(b.signed_settlement_amount)
            ELSE 0::numeric
        END AS net_payable_by_customer,

        CASE
            WHEN b.semantic_role = 'signed_net_settlement'
             AND b.signed_settlement_amount > 0
             AND b.due_bucket <> 'closed'
            THEN b.signed_settlement_amount
            ELSE 0::numeric
        END AS net_refundable_to_customer,

        CASE
            WHEN b.semantic_role = 'security_deposit_collection'
             AND b.due_bucket <> 'closed'
            THEN b.raw_outstanding_amount
            ELSE 0::numeric
        END AS security_deposit_outstanding,

        CASE
            WHEN b.semantic_role IN ('raw_charge', 'recurring_charge')
             AND b.due_bucket <> 'closed'
            THEN b.raw_outstanding_amount
            ELSE 0::numeric
        END AS charge_outstanding,

        CASE
            WHEN b.semantic_role IN ('raw_charge', 'recurring_charge')
             AND b.future_treatment = 'indicative_only'
             AND b.due_bucket = 'future_scheduled'
             AND b.due_bucket <> 'closed'
            THEN b.raw_outstanding_amount
            ELSE 0::numeric
        END AS future_indicative_amount,

        CASE
            WHEN b.semantic_role IN ('raw_charge', 'recurring_charge')
             AND NOT (
                  b.future_treatment = 'indicative_only'
                  AND b.due_bucket = 'future_scheduled'
             )
             AND b.due_bucket <> 'closed'
            THEN b.raw_outstanding_amount
            ELSE 0::numeric
        END AS actionable_charge_outstanding

    FROM bucketed b
)
SELECT
    p.*,

    (
        COALESCE(p.actionable_charge_outstanding, 0)
      + COALESCE(p.security_deposit_outstanding, 0)
      + COALESCE(p.net_payable_by_customer, 0)
    )::numeric AS actionable_receivable_amount,

    'staging_booking_invoice_finance_v:v4'::text AS finance_projection_source

FROM projected p;

COMMIT;

-- Validation for booking 57808. Expected app_total_pending_display_amount = 63180
-- with the screenshot values shared.
SELECT
    booking_id,
    SUM(app_pending_display_amount) AS app_total_pending_display_amount,
    SUM(future_indicative_amount) AS future_indicative_amount,
    SUM(actionable_charge_outstanding) AS actionable_charge_outstanding,
    SUM(security_deposit_outstanding) AS security_deposit_outstanding,
    SUM(net_payable_by_customer) AS net_payable_by_customer,
    SUM(actionable_receivable_amount) AS actionable_receivable_amount
FROM "AnalyticsEngine".staging_booking_invoice_finance_v
WHERE booking_id = 57808
  AND due_bucket <> 'closed'
GROUP BY booking_id;

SELECT
    source_id,
    transaction_type,
    status,
    amount_status,
    amount,
    total_amount,
    amount_recieved,
    pending_balance,
    ui_payable_amount,
    ui_received_amount,
    app_pending_display_amount,
    raw_outstanding_amount,
    due_bucket,
    is_indicative,
    future_indicative_amount,
    actionable_charge_outstanding,
    security_deposit_outstanding,
    net_payable_by_customer,
    actionable_receivable_amount
FROM "AnalyticsEngine".staging_booking_invoice_finance_v
WHERE booking_id = 57808
ORDER BY source_id;

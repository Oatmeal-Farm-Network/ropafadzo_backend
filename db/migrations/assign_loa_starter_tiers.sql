-- Put Livestock of America ranchers on the new packages.
--
-- Scope is deliberately narrow. Business.SubscriptionTier is shared with
-- Oatmeal Farm Network, where ~1,653 businesses sit on the legacy tier 'basic'
-- and 47 on 'Free'. Neither resolves to a package, so those businesses fall
-- through to the site-wide feature defaults and are unrestricted today.
-- Assigning them an LOA package would take features away from OFN customers,
-- so this script only touches businesses that actually use LOA: the ones with
-- animal records (~75).
--
--   every LOA rancher      -> Livestock Starter
--   Alpacas at Lone Ranch  -> Livestock Enterprise   (BusinessID 14)
--
-- Reversible: previous values are copied to LOASubscriptionTierBackup before
-- anything changes. To undo:
--     UPDATE b SET b.SubscriptionTier = k.OldTier,
--                  b.SubscriptionStatus = k.OldStatus
--     FROM Business b
--     JOIN LOASubscriptionTierBackup k ON k.BusinessID = b.BusinessID;
--
-- Idempotent: re-running re-backs-up nothing new (the backup keeps the FIRST
-- recorded value per business) and re-applies the same tiers.

-- ── 1. Backup table ─────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_NAME = 'LOASubscriptionTierBackup')
    CREATE TABLE LOASubscriptionTierBackup (
        BusinessID   INT PRIMARY KEY,
        OldTier      VARCHAR(100) NULL,
        OldStatus    VARCHAR(50)  NULL,
        BackedUpAt   DATETIME2 DEFAULT GETDATE()
    );
GO

-- ── 2. Who counts as an LOA rancher ─────────────────────────────────────────
DECLARE @loa TABLE (BusinessID INT PRIMARY KEY);
INSERT INTO @loa (BusinessID)
SELECT DISTINCT BusinessID FROM Animals WHERE BusinessID IS NOT NULL;

-- Alpacas at Lone Ranch gets Enterprise whether or not it has animals listed.
IF NOT EXISTS (SELECT 1 FROM @loa WHERE BusinessID = 14)
    INSERT INTO @loa (BusinessID) VALUES (14);

-- ── 3. Record what they were on first ───────────────────────────────────────
INSERT INTO LOASubscriptionTierBackup (BusinessID, OldTier, OldStatus)
SELECT b.BusinessID, b.SubscriptionTier, b.SubscriptionStatus
FROM Business b
JOIN @loa l ON l.BusinessID = b.BusinessID
WHERE NOT EXISTS (SELECT 1 FROM LOASubscriptionTierBackup k
                  WHERE k.BusinessID = b.BusinessID);

-- ── 4. Apply ────────────────────────────────────────────────────────────────
UPDATE b
SET b.SubscriptionTier   = 'Livestock Starter',
    b.SubscriptionStatus = 'active'
FROM Business b
JOIN @loa l ON l.BusinessID = b.BusinessID
WHERE b.BusinessID <> 14;

UPDATE Business
SET SubscriptionTier   = 'Livestock Enterprise',
    SubscriptionStatus = 'active'
WHERE BusinessID = 14;
GO

-- ── 5. What changed ─────────────────────────────────────────────────────────
SELECT SubscriptionTier, COUNT(*) AS Businesses
FROM Business
WHERE SubscriptionTier LIKE 'Livestock %'
GROUP BY SubscriptionTier
ORDER BY SubscriptionTier;

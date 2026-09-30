-- Livestock of America subscription packages: Starter / Pro / Enterprise.
--
-- Creates the three LOA packages, maps each to the features it includes, and
-- records the per-plan listing allowances.
--
-- Package names are deliberately NOT 'Free' or 'basic'. Businesses resolve to a
-- package by name (Business.SubscriptionTier = SubscriptionPackage.PackageName),
-- and the shared database holds ~1,653 legacy 'basic' rows plus 47 'Free' ones.
-- Reusing either name would retroactively cap every one of those businesses,
-- most of which are Oatmeal Farm Network accounts.
--
-- Allowances: NULL means unlimited, 0 excludes the listing type entirely.
--   Starter     5 for-sale, 5 studs, 1 directory, 5 equipment, 0 jobs, 1 service
--   Pro        10 for-sale, 10 studs, 1 directory, 10 equipment, 5 jobs, unlimited services
--   Enterprise unlimited across the board
--
-- Idempotent: re-running updates the same rows instead of duplicating them.
-- Tier assignment lives in a separate script (assign_loa_starter_tiers.sql) so
-- the catalog can be created without touching any business.

-- ── 1. Listing allowances this catalog needs ────────────────────────────────
-- MaxForSaleListings / MaxStudListings / MaxDirectoryListings already exist.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'MaxEquipmentListings')
    ALTER TABLE SubscriptionPackage ADD MaxEquipmentListings INT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'MaxJobPostings')
    ALTER TABLE SubscriptionPackage ADD MaxJobPostings INT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'MaxServiceListings')
    ALTER TABLE SubscriptionPackage ADD MaxServiceListings INT NULL;
GO

-- ── 2. herd_health is the one service in the matrix with no feature row ─────
IF NOT EXISTS (SELECT 1 FROM CompanySiteManagement WHERE FeatureKey = 'herd_health')
    INSERT INTO CompanySiteManagement (FeatureKey, FeatureName, IsEnabled,
                                       MonthlyPrice, YearlyPrice, SortOrder)
    VALUES ('herd_health', 'Herd Health', 1, 0.0, 0.0, 5);
GO

-- ── 3. The three packages ───────────────────────────────────────────────────
MERGE SubscriptionPackage AS target
USING (VALUES
    ('Livestock Starter',
     'Be found on Livestock of America. List a few animals and studs, claim your directory spot, and browse the marketplace, events and job board.',
     0.00, 0.00, 10, 5, 5, 1, 5, 0, 1),
    ('Livestock Pro',
     'For a working ranch: sell without the Starter caps, run herd health, accounting and a storefront, and host your own events.',
     49.00, 490.00, 11, 10, 10, 1, 10, 5, NULL),
    ('Livestock Enterprise',
     'For associations, aggregators and multi-ranch operations: unlimited listings, your own website, advanced CSA, and every service on the platform.',
     299.00, 2990.00, 12, NULL, NULL, NULL, NULL, NULL, NULL)
) AS source (PackageName, Description, MonthlyPrice, YearlyPrice, SortOrder,
             MaxForSaleListings, MaxStudListings, MaxDirectoryListings,
             MaxEquipmentListings, MaxJobPostings, MaxServiceListings)
ON target.PackageName = source.PackageName
WHEN MATCHED THEN UPDATE SET
    Description           = source.Description,
    MonthlyPrice          = source.MonthlyPrice,
    YearlyPrice           = source.YearlyPrice,
    SortOrder             = source.SortOrder,
    MaxForSaleListings    = source.MaxForSaleListings,
    MaxStudListings       = source.MaxStudListings,
    MaxDirectoryListings  = source.MaxDirectoryListings,
    MaxEquipmentListings  = source.MaxEquipmentListings,
    MaxJobPostings        = source.MaxJobPostings,
    MaxServiceListings    = source.MaxServiceListings,
    IsActive              = 1,
    UpdatedAt             = GETDATE()
WHEN NOT MATCHED THEN INSERT
    (PackageName, Description, MonthlyPrice, YearlyPrice, IsActive, SortOrder,
     MaxForSaleListings, MaxStudListings, MaxDirectoryListings,
     MaxEquipmentListings, MaxJobPostings, MaxServiceListings, CreatedAt)
    VALUES
    (source.PackageName, source.Description, source.MonthlyPrice, source.YearlyPrice, 1, source.SortOrder,
     source.MaxForSaleListings, source.MaxStudListings, source.MaxDirectoryListings,
     source.MaxEquipmentListings, source.MaxJobPostings, source.MaxServiceListings, GETDATE());
GO

-- ── 4. Which features each package turns on ─────────────────────────────────
-- Rebuilt from scratch for these three packages only, so the mapping always
-- matches the list below and re-running cannot leave stale rows behind.
DECLARE @pkg TABLE (PackageName VARCHAR(100), FeatureKey VARCHAR(100));

-- Starter: presence and discovery.
INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Livestock Starter', k FROM (VALUES
    ('livestock'), ('business_directory'), ('associations'), ('blog'),
    ('commodity_prices'), ('equipment'), ('events'), ('food_system_newsfeed'),
    ('forums'), ('grants_programs'), ('job_board'), ('land_leasing'),
    ('properties'), ('services'), ('testimonials')
) AS v(k);

-- Pro: everything in Starter, plus the tools a working ranch runs on.
INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Livestock Pro', k FROM (VALUES
    ('livestock'), ('herd_health'), ('business_directory'), ('associations'),
    ('blog'), ('certifications'), ('commodity_prices'), ('csa_management'),
    ('equipment'), ('events'), ('food_system_newsfeed'), ('forums'),
    ('grants_programs'), ('job_board'), ('land_leasing'), ('properties'),
    ('services'), ('testimonials'), ('accounting'), ('products')
) AS v(k);

-- Enterprise: every service in the matrix.
-- csa_management is included alongside csa_advanced: the advanced features
-- extend the basic CSA tools rather than replacing them, so Enterprise would
-- otherwise lose functionality Pro has.
INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Livestock Enterprise', k FROM (VALUES
    ('livestock'), ('herd_health'), ('business_directory'), ('associations'),
    ('blog'), ('certifications'), ('commodity_prices'), ('csa_management'),
    ('csa_advanced'), ('equipment'), ('events'), ('food_system_newsfeed'),
    ('forums'), ('grants_programs'), ('job_board'), ('land_leasing'),
    ('properties'), ('services'), ('testimonials'), ('accounting'),
    ('products'), ('my_website')
) AS v(k);

DELETE spf
FROM SubscriptionPackageFeature spf
JOIN SubscriptionPackage sp ON sp.PackageID = spf.PackageID
WHERE sp.PackageName IN ('Livestock Starter', 'Livestock Pro', 'Livestock Enterprise');

INSERT INTO SubscriptionPackageFeature (PackageID, FeatureID)
SELECT sp.PackageID, csm.FeatureID
FROM @pkg p
JOIN SubscriptionPackage sp   ON sp.PackageName = p.PackageName
JOIN CompanySiteManagement csm ON csm.FeatureKey = p.FeatureKey;
GO

-- ── 5. What was created ─────────────────────────────────────────────────────
SELECT sp.PackageName, sp.MonthlyPrice, sp.YearlyPrice,
       sp.MaxForSaleListings, sp.MaxStudListings, sp.MaxEquipmentListings,
       sp.MaxJobPostings, sp.MaxServiceListings,
       COUNT(spf.FeatureID) AS Features
FROM SubscriptionPackage sp
LEFT JOIN SubscriptionPackageFeature spf ON spf.PackageID = sp.PackageID
WHERE sp.PackageName LIKE 'Livestock %'
GROUP BY sp.PackageName, sp.MonthlyPrice, sp.YearlyPrice,
         sp.MaxForSaleListings, sp.MaxStudListings, sp.MaxEquipmentListings,
         sp.MaxJobPostings, sp.MaxServiceListings
ORDER BY sp.PackageName;

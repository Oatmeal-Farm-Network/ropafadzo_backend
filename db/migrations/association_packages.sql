-- Association plans for Livestock of America: Basic / Plus / Show.
--
-- Associations are billed differently from ranches: they pay on behalf of their
-- members, and the thing they actually buy at the top end is the ability to run
-- a full ag show. These three are tagged with the Agricultural Association
-- business type, so the signup picker offers them instead of the general
-- Livestock plans (see the type preference in platform_subscriptions).
--
--   Association Basic  free        presence: profile, directory, newsfeed
--   Association Plus   $99/$990    members, meetings, website, accounting,
--                                  and up to 2 events a year
--   Association Show   $299/$2990  unlimited events plus the full show suite
--
-- Splitting the events switch
-- ---------------------------
-- Subscriptions had a single 'events' feature, so a plan could only turn events
-- on or off. The show modules that already exist -- classes and divisions,
-- halter, fleece, spinoff, fibre arts, auction, vendor fair, booths, floor plan,
-- QR check-in, sponsorship, COI, promo codes, waitlists, mailing list, judge and
-- speaker portals, results and exports -- now sit behind their own feature,
-- event_show_suite, which only the Show plan carries.
--
-- MaxEventsPerYear caps how many events a plan may create in a calendar year.
-- NULL means unlimited, matching the other allowances.
--
-- Idempotent.

-- ── 1. Events-per-year allowance ────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'MaxEventsPerYear')
    ALTER TABLE SubscriptionPackage ADD MaxEventsPerYear INT NULL;
GO

-- ── 2. The show-suite feature ───────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM CompanySiteManagement WHERE FeatureKey = 'event_show_suite')
    INSERT INTO CompanySiteManagement (FeatureKey, FeatureName, IsEnabled,
                                       MonthlyPrice, YearlyPrice, SortOrder)
    VALUES ('event_show_suite', 'Full Show Suite (classes, auction, check-in)', 1, 0.0, 0.0, 8);
GO

-- ── 3. The three plans ──────────────────────────────────────────────────────
DECLARE @assoc INT = (SELECT TOP 1 BusinessTypeID FROM BusinessTypeLookup
                      WHERE BusinessType LIKE '%Association%' ORDER BY BusinessTypeID);

MERGE SubscriptionPackage AS target
USING (VALUES
    ('Association Basic',
     'Get your association found on Livestock of America: your profile, a directory listing for the club, and the food system newsfeed. Members can browse and register for events run by others.',
     0.00, 0.00, 20, 0, NULL),
    ('Association Plus',
     'For an association that runs itself here: member management, meetings and minutes, your own website, dues and accounting, and up to two of your own events a year with online registration.',
     99.00, 990.00, 21, 2, NULL),
    ('Association Show',
     'Everything in Plus, with unlimited events and the full show suite: classes and divisions, halter, fleece and spinoff competitions, auctions, vendor fair and booths, floor plans, QR check-in, sponsorships and COI, promo codes and waitlists, judge and speaker portals, results and exports.',
     299.00, 2990.00, 22, NULL, NULL)
) AS source (PackageName, Description, MonthlyPrice, YearlyPrice, SortOrder,
             MaxEventsPerYear, MaxDirectoryListings)
ON target.PackageName = source.PackageName
WHEN MATCHED THEN UPDATE SET
    Description      = source.Description,
    MonthlyPrice     = source.MonthlyPrice,
    YearlyPrice      = source.YearlyPrice,
    SortOrder        = source.SortOrder,
    MaxEventsPerYear = source.MaxEventsPerYear,
    BusinessTypeID   = @assoc,
    IsActive         = 1,
    IsSelfService    = 1,
    UpdatedAt        = GETDATE()
WHEN NOT MATCHED THEN INSERT
    (PackageName, Description, MonthlyPrice, YearlyPrice, SortOrder,
     MaxEventsPerYear, BusinessTypeID, IsActive, IsSelfService, CreatedAt)
    VALUES
    (source.PackageName, source.Description, source.MonthlyPrice, source.YearlyPrice,
     source.SortOrder, source.MaxEventsPerYear, @assoc, 1, 1, GETDATE());
GO

-- ── 4. Features per plan ────────────────────────────────────────────────────
DECLARE @pkg TABLE (PackageName VARCHAR(100), FeatureKey VARCHAR(100));

INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Association Basic', k FROM (VALUES
    ('associations'), ('business_directory'), ('food_system_newsfeed'),
    ('events'), ('blog'), ('testimonials'), ('forums')
) AS v(k);

INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Association Plus', k FROM (VALUES
    ('associations'), ('business_directory'), ('food_system_newsfeed'),
    ('events'), ('blog'), ('testimonials'), ('forums'),
    ('meetings'), ('my_website'), ('accounting'), ('document_vault'),
    ('livestock'), ('commodity_prices'), ('grants_programs'), ('education_center')
) AS v(k);

INSERT INTO @pkg (PackageName, FeatureKey)
SELECT 'Association Show', k FROM (VALUES
    ('associations'), ('business_directory'), ('food_system_newsfeed'),
    ('events'), ('event_show_suite'), ('blog'), ('testimonials'), ('forums'),
    ('meetings'), ('my_website'), ('accounting'), ('document_vault'),
    ('livestock'), ('commodity_prices'), ('grants_programs'), ('education_center'),
    ('products'), ('services')
) AS v(k);

DELETE spf
FROM SubscriptionPackageFeature spf
JOIN SubscriptionPackage sp ON sp.PackageID = spf.PackageID
WHERE sp.PackageName IN ('Association Basic', 'Association Plus', 'Association Show');

INSERT INTO SubscriptionPackageFeature (PackageID, FeatureID)
SELECT sp.PackageID, csm.FeatureID
FROM @pkg p
JOIN SubscriptionPackage sp    ON sp.PackageName = p.PackageName
JOIN CompanySiteManagement csm ON csm.FeatureKey = p.FeatureKey;
GO

-- ── 5. Livestock of America only ────────────────────────────────────────────
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
           WHERE TABLE_NAME = 'SubscriptionPackageSite')
    INSERT INTO SubscriptionPackageSite (PackageID, SiteKey)
    SELECT p.PackageID, 'loa'
    FROM SubscriptionPackage p
    WHERE p.PackageName IN ('Association Basic', 'Association Plus', 'Association Show')
      AND NOT EXISTS (SELECT 1 FROM SubscriptionPackageSite s
                      WHERE s.PackageID = p.PackageID AND s.SiteKey = 'loa');
GO

SELECT sp.PackageName, sp.MonthlyPrice, sp.YearlyPrice, sp.MaxEventsPerYear,
       sp.IsActive, sp.IsSelfService, bt.BusinessType,
       COUNT(spf.FeatureID) AS Features
FROM SubscriptionPackage sp
LEFT JOIN SubscriptionPackageFeature spf ON spf.PackageID = sp.PackageID
LEFT JOIN BusinessTypeLookup bt ON bt.BusinessTypeID = sp.BusinessTypeID
WHERE sp.PackageName LIKE 'Association %'
GROUP BY sp.PackageName, sp.MonthlyPrice, sp.YearlyPrice, sp.MaxEventsPerYear,
         sp.IsActive, sp.IsSelfService, bt.BusinessType
ORDER BY sp.PackageName;

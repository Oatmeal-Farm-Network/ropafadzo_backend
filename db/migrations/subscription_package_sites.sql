-- Which storefronts each subscription package is offered on.
--
-- One database serves four sites: Livestock of America, Livestock of India,
-- Oatmeal Farm Network and Oatmeal Farm Network India. Until now every active
-- package showed on all of them, so the LOA plans would appear in OFN signup
-- and vice versa.
--
-- A package with NO rows here is offered everywhere. That keeps every existing
-- package behaving exactly as it does today, and means only packages that
-- should be restricted need rows.
--
-- The admin screen at oatmeal-ai.com/app/admin/subscriptions writes this table,
-- and creates it on first use; this script exists so the table and the LOA
-- restriction can be put in place without opening that screen.
--
-- Idempotent.

IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_NAME = 'SubscriptionPackageSite')
    CREATE TABLE SubscriptionPackageSite (
        PackageID INT NOT NULL,
        SiteKey   VARCHAR(32) NOT NULL,
        PRIMARY KEY (PackageID, SiteKey)
    );
GO

-- The three Livestock of America plans belong to LOA only. Without these rows
-- they would be offered on the OFN sites too.
INSERT INTO SubscriptionPackageSite (PackageID, SiteKey)
SELECT p.PackageID, 'loa'
FROM SubscriptionPackage p
WHERE p.PackageName IN ('Livestock Starter', 'Livestock Pro', 'Livestock Enterprise')
  AND NOT EXISTS (SELECT 1 FROM SubscriptionPackageSite s
                  WHERE s.PackageID = p.PackageID AND s.SiteKey = 'loa');
GO

SELECT p.PackageName,
       ISNULL(STRING_AGG(s.SiteKey, ', '), '(all sites)') AS Sites
FROM SubscriptionPackage p
LEFT JOIN SubscriptionPackageSite s ON s.PackageID = p.PackageID
WHERE p.IsActive = 1
GROUP BY p.PackageName
ORDER BY p.PackageName;

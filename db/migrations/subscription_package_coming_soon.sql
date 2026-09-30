-- "Coming Soon" state for a subscription package.
--
-- IsActive already means "usable": feature resolution and the plan limits both
-- join on IsActive = 1, so switching it off takes features away from every
-- business already on that package. It cannot double as "advertise it but do
-- not sell it yet".
--
-- IsComingSoon is that third state. A package with it set still appears in the
-- signup picker, labelled Coming Soon, but cannot be chosen: assign-package and
-- package-checkout both refuse it. Businesses already on the package are
-- unaffected, so it is safe to set on a live plan.
--
--   IsActive = 1, IsComingSoon = 0   sellable now (the default)
--   IsActive = 1, IsComingSoon = 1   shown as Coming Soon, not sellable
--   IsActive = 0                     hidden everywhere, as before
--
-- Idempotent.

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'IsComingSoon')
    ALTER TABLE SubscriptionPackage
        ADD IsComingSoon BIT NOT NULL CONSTRAINT DF_SubscriptionPackage_IsComingSoon DEFAULT 0;
GO

SELECT PackageName, IsActive, IsSelfService, IsComingSoon, MonthlyPrice
FROM SubscriptionPackage
ORDER BY SortOrder, PackageName;

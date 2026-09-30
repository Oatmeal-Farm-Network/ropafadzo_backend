-- Mark which subscription packages a member may pick for themselves.
--
-- /api/platform-subscriptions/packages returns every active package, and any
-- package with a NULL BusinessTypeID shows for every business type. That is
-- fine for the admin assignment screen, but the LOA signup flow renders the
-- same list, which would let a new member choose an internal package -- most
-- damagingly 'Everything', which costs nothing and turns on all 33 features.
--
-- Deactivating those packages is not an option: feature resolution joins on
-- IsActive = 1, so switching it off would strip features from the businesses
-- already assigned to them ('Everything' 3, 'Livestock Association Package' 3,
-- 'Food Aggregator' 1).
--
-- Instead every package gets IsSelfService, defaulting to 0, and only the three
-- LOA plans are opted in. Callers that do not ask for self-service packages
-- (the OFN admin screens) see the list unchanged.
--
-- Idempotent.

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('SubscriptionPackage')
                 AND name = 'IsSelfService')
    ALTER TABLE SubscriptionPackage
        ADD IsSelfService BIT NOT NULL CONSTRAINT DF_SubscriptionPackage_IsSelfService DEFAULT 0;
GO

UPDATE SubscriptionPackage
SET IsSelfService = 1
WHERE PackageName IN ('Livestock Starter', 'Livestock Pro', 'Livestock Enterprise');

-- Superseded by Livestock Starter and assigned to no business, so it should not
-- appear anywhere. Safe to deactivate for that reason.
UPDATE SubscriptionPackage
SET IsActive = 0
WHERE PackageName = 'Basic Rancher Package'
  AND NOT EXISTS (SELECT 1 FROM Business b
                  WHERE b.SubscriptionTier = 'Basic Rancher Package');
GO

SELECT PackageName, IsActive, IsSelfService, MonthlyPrice, YearlyPrice
FROM SubscriptionPackage
ORDER BY IsSelfService DESC, SortOrder, PackageName;

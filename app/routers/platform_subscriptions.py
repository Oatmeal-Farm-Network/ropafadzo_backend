# routers/platform_subscriptions.py
# OFN platform subscriptions — Stripe Checkout for per-Business monthly billing.
# Mount: app.include_router(platform_subscriptions_router)
#
# Plans live in SubscriptionLevels (StripeAPIID = live price, StripeAPIIDTest =
# test price). Per-business state is stored on Business:
#   StripeCustomerID, StripeSubscriptionID, SubscriptionLevel (FK to
#   SubscriptionLevels.SubscriptionID), SubscriptionStatus,
#   SubscriptionstartDate, SubscriptionEndDate, SubscriptionTier.
#
# Lifecycle:
#   /checkout → Stripe Checkout Session → buyer completes payment →
#   checkout.session.completed webhook writes StripeCustomerID +
#   StripeSubscriptionID → customer.subscription.updated keeps status in sync.

import os
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.core.jwt_auth import get_current_user
from app.routers.platform_settings import get_stripe_config, _is_admin


platform_subscriptions_router = APIRouter(prefix="/api/platform-subscriptions", tags=["platform-subscriptions"])

OFN_BASE_URL = os.getenv("OFN_BASE_URL", "https://oatmealfarmnetwork.com")
# Where Stripe returns the member after package checkout. Separate from
# OFN_BASE_URL because this backend also serves Livestock of America.
SITE_BASE_URL = os.getenv("LOA_FRONTEND_URL") or os.getenv("FRONTEND_URL", "https://livestockofamerica.com")
SITE_BASE_URL = SITE_BASE_URL.rstrip("/")


def _stripe(db: Session):
    cfg = get_stripe_config(db)
    if not cfg.get("StripeSecretKey"):
        raise HTTPException(503, "Stripe not configured. Ask an admin to add keys in Accounting → Payments.")
    import stripe
    stripe.api_key = cfg["StripeSecretKey"]
    return stripe, cfg


def _mode_from_cfg(cfg: dict) -> str:
    # OFNPlatformSettings stores StripeTestMode as a bit; treat missing as test.
    raw = cfg.get("StripeTestMode")
    is_test = True if raw is None else bool(raw)
    return "test" if is_test else "live"


def _require_business_access(db: Session, people_id: str, business_id: int):
    row = db.execute(
        text("SELECT 1 FROM BusinessAccess WHERE PeopleID = :pid AND BusinessID = :bid AND Active = 1"),
        {"pid": int(people_id), "bid": business_id},
    ).fetchone()
    if not row:
        raise HTTPException(403, "You do not have access to this business.")


def _price_id_for(level: dict, mode: str) -> Optional[str]:
    # mode is 'live' or 'test' from OFNPlatformSettings.StripeMode.
    return level.get("StripeAPIID") if mode == "live" else level.get("StripeAPIIDTest")


# ─────────────────────────────────────────────────────────────────────────────
# READS
# ─────────────────────────────────────────────────────────────────────────────

@platform_subscriptions_router.get("/plans")
def list_plans(country_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Return active subscription plans. Filters by country_id when provided,
    otherwise returns all. Only plans with a Stripe price ID for the current
    mode are returned as selectable; the rest are returned with
    selectable=False so the UI can show them as coming-soon."""
    _, cfg = _stripe(db)
    mode = _mode_from_cfg(cfg)

    sql = "SELECT * FROM SubscriptionLevels WHERE 1=1"
    params = {}
    if country_id is not None:
        sql += " AND country_id = :cid"
        params["cid"] = country_id
    sql += " ORDER BY SubscriptionMonthlyRate, SubscriptionID"

    rows = db.execute(text(sql), params).mappings().fetchall()
    plans = []
    for r in rows:
        level = dict(r)
        for k in ("SubscriptionMonthlyRate",):
            if level.get(k) is not None:
                level[k] = float(level[k])
        price_id = _price_id_for(level, mode)
        level["selectable"] = bool(price_id)
        level["price_id"] = price_id
        plans.append(level)
    return {"mode": mode, "plans": plans}


@platform_subscriptions_router.get("/current/{business_id}")
def current_subscription(
    business_id: int,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    _require_business_access(db, people_id, business_id)
    row = db.execute(
        text("""
            SELECT b.BusinessID, b.BusinessName, b.SubscriptionLevel,
                   b.SubscriptionStatus, b.SubscriptionstartDate, b.SubscriptionEndDate,
                   b.SubscriptionTier, b.StripeCustomerID, b.StripeSubscriptionID,
                   sl.SubscriptionTitle, sl.SubscriptionMonthlyRate
            FROM Business b
            LEFT JOIN SubscriptionLevels sl ON b.SubscriptionLevel = sl.SubscriptionID
            WHERE b.BusinessID = :bid
        """),
        {"bid": business_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Business not found")
    data = dict(row._mapping)
    if data.get("SubscriptionMonthlyRate") is not None:
        data["SubscriptionMonthlyRate"] = float(data["SubscriptionMonthlyRate"])
    return data


# ─────────────────────────────────────────────────────────────────────────────
# WRITES
# ─────────────────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    subscription_id: int  # SubscriptionLevels.SubscriptionID


@platform_subscriptions_router.post("/checkout/{business_id}")
def start_checkout(
    business_id: int,
    req: CheckoutRequest,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    _require_business_access(db, people_id, business_id)

    stripe, cfg = _stripe(db)
    mode = _mode_from_cfg(cfg)

    level_row = db.execute(
        text("SELECT * FROM SubscriptionLevels WHERE SubscriptionID = :id"),
        {"id": req.subscription_id},
    ).mappings().fetchone()
    if not level_row:
        raise HTTPException(404, "Plan not found")
    level = dict(level_row)
    price_id = _price_id_for(level, mode)
    if not price_id:
        raise HTTPException(
            400,
            f"Plan '{level.get('SubscriptionTitle')}' is not yet available for {mode} payments. "
            "Contact an admin to add a Stripe price.",
        )

    business = db.execute(
        text("SELECT BusinessID, BusinessName, BusinessEmail, StripeCustomerID FROM Business WHERE BusinessID = :bid"),
        {"bid": business_id},
    ).mappings().fetchone()
    if not business:
        raise HTTPException(404, "Business not found")

    customer_id = business.get("StripeCustomerID")
    if not customer_id:
        customer = stripe.Customer.create(
            email=business.get("BusinessEmail") or None,
            name=business.get("BusinessName") or None,
            metadata={"business_id": str(business_id)},
        )
        customer_id = customer.id
        db.execute(
            text("UPDATE Business SET StripeCustomerID = :cid WHERE BusinessID = :bid"),
            {"cid": customer_id, "bid": business_id},
        )
        db.commit()

    success_url = f"{OFN_BASE_URL}/account/subscription?BusinessID={business_id}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{OFN_BASE_URL}/account/subscription?BusinessID={business_id}&cancelled=1"

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={
            "business_id": str(business_id),
            "subscription_level_id": str(req.subscription_id),
        },
        subscription_data={
            "metadata": {
                "business_id": str(business_id),
                "subscription_level_id": str(req.subscription_id),
            },
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"checkout_url": session.url, "session_id": session.id}


# ─────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTION PACKAGES — canonical plan list managed via the oatmeal_main
# admin UI (http://localhost:8080/app/admin/subscriptions). Writes go to the
# shared SubscriptionPackage table.
# ─────────────────────────────────────────────────────────────────────────────

# Columns and tables that arrive with a migration. Only a positive answer is
# remembered: caching "missing" would keep a running instance ignoring the
# column for its whole life, so a migration would need a restart to take effect
# -- which is exactly what happened when IsSelfService and
# SubscriptionPackageSite were first applied in production.
_SCHEMA_PRESENT: set = set()


def _has_package_column(db: Session, column: str) -> bool:
    """Whether SubscriptionPackage.<column> exists yet."""
    key = f"col:{column}"
    if key in _SCHEMA_PRESENT:
        return True
    found = bool(db.execute(text(
        "SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('SubscriptionPackage') "
        "AND name = :col"
    ), {"col": column}).scalar())
    if found:
        _SCHEMA_PRESENT.add(key)
    return found


def _has_self_service_column(db: Session) -> bool:
    """Whether the IsSelfService column from its migration is in place."""
    return _has_package_column(db, "IsSelfService")


def _has_coming_soon_column(db: Session) -> bool:
    """Whether the IsComingSoon column from its migration is in place."""
    return _has_package_column(db, "IsComingSoon")


def _has_site_table(db: Session) -> bool:
    """Whether SubscriptionPackageSite exists yet.

    The admin screen on oatmeal-ai.com creates it on first use; until then no
    package is restricted to a site and the filter is skipped.
    """
    if "tbl:SubscriptionPackageSite" in _SCHEMA_PRESENT:
        return True
    found = bool(db.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_NAME = 'SubscriptionPackageSite'"
    )).scalar())
    if found:
        _SCHEMA_PRESENT.add("tbl:SubscriptionPackageSite")
    return found


@platform_subscriptions_router.get("/packages")
def list_packages(
    business_type_id: Optional[int] = None,
    self_service: bool = False,
    site: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Active subscription packages from the SubscriptionPackage table.

    business_type_id prefers plans built for that account type: when any exist,
    only those come back, so an association is offered association plans rather
    than those plus the general ones. A package with a NULL BusinessTypeID
    applies to every type and is the fallback when the chosen type has none of
    its own.

    site ('loa', 'loa_india', 'ofn', 'ofn_india') narrows it to packages that
    storefront offers, as set on the Subscriptions admin screen.

    self_service=true narrows the list to packages a member may choose during
    signup. Without it the full list comes back, which is what the admin
    assignment screens want -- they hand out internal packages like
    'Everything' that must never appear in the signup picker.

    Returns the current payment mode so the frontend can gate the pay step."""
    cfg = get_stripe_config(db)
    mode = _mode_from_cfg(cfg)

    coming_soon_col = "p.IsComingSoon," if _has_coming_soon_column(db) else ""
    # Allowances added after the original three; each arrives with a migration,
    # so only ask for the ones that are actually there.
    allowance_cols = "".join(
        f"p.{c}, " for c in ("MaxEquipmentListings", "MaxJobPostings",
                             "MaxServiceListings", "MaxEventsPerYear")
        if _has_package_column(db, c)
    )
    sql = """
        SELECT p.PackageID, p.PackageName, p.Description, p.BusinessTypeID,
               p.MonthlyPrice, p.YearlyPrice, p.SortOrder,
               p.MaxForSaleListings, p.MaxStudListings, p.MaxDirectoryListings,
               {allowance_cols}
               {coming_soon_col}
               bt.BusinessType
        FROM SubscriptionPackage p
        LEFT JOIN BusinessTypeLookup bt ON p.BusinessTypeID = bt.BusinessTypeID
        WHERE p.IsActive = 1
    """.replace("{coming_soon_col}", coming_soon_col).replace("{allowance_cols}", allowance_cols)
    params = {}
    if business_type_id is not None:
        sql += " AND (p.BusinessTypeID = :btid OR p.BusinessTypeID IS NULL)"
        params["btid"] = business_type_id
    if self_service and _has_self_service_column(db):
        sql += " AND p.IsSelfService = 1"
    if site and _has_site_table(db):
        # A package with no rows in SubscriptionPackageSite is offered on every
        # site, which is how packages behaved before the admin screen could
        # restrict them.
        sql += (" AND (NOT EXISTS (SELECT 1 FROM SubscriptionPackageSite s"
                "                  WHERE s.PackageID = p.PackageID)"
                "     OR EXISTS (SELECT 1 FROM SubscriptionPackageSite s"
                "                WHERE s.PackageID = p.PackageID AND s.SiteKey = :site))")
        params["site"] = site
    sql += " ORDER BY p.SortOrder, p.PackageName"

    rows = db.execute(text(sql), params).mappings().fetchall()

    # Plans built for this account type win outright. The SQL above also let
    # through the type-less plans, which is right only when the type has none of
    # its own -- otherwise an association would see the general plans alongside
    # its own, which is not what "plans for this account type" means.
    if business_type_id is not None:
        for_type = [r for r in rows if r["BusinessTypeID"] == business_type_id]
        if for_type:
            rows = for_type

    coming_soon_known = _has_coming_soon_column(db)
    packages = []
    for r in rows:
        pkg = dict(r)
        for k in ("MonthlyPrice", "YearlyPrice"):
            if pkg.get(k) is not None:
                pkg[k] = float(pkg[k])
        # Shown in the picker but not sellable. Always present so the frontend
        # can rely on it, false while the column has not been added yet.
        pkg["IsComingSoon"] = bool(pkg.get("IsComingSoon")) if coming_soon_known else False
        packages.append(pkg)

    # What each package includes, so the picker can lay the plans side by side
    # instead of describing them one card at a time.
    if packages:
        ids = [p["PackageID"] for p in packages]
        feature_rows = db.execute(text("""
            SELECT spf.PackageID, csm.FeatureKey, csm.FeatureName, csm.SortOrder
            FROM SubscriptionPackageFeature spf
            JOIN CompanySiteManagement csm ON csm.FeatureID = spf.FeatureID
            WHERE spf.PackageID IN :ids
            ORDER BY csm.SortOrder
        """).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).fetchall()
        by_package: dict = {}
        catalog: dict = {}
        for pid, key, name, sort in feature_rows:
            by_package.setdefault(pid, []).append(key)
            catalog.setdefault(key, {"feature_key": key, "feature_name": name,
                                     "sort_order": sort})
        for p in packages:
            p["features"] = by_package.get(p["PackageID"], [])
        # The union, in display order: the rows of that comparison.
        features_index = sorted(catalog.values(), key=lambda f: (f["sort_order"], f["feature_name"]))
    else:
        features_index = []
    return {"mode": mode, "packages": packages, "features": features_index}


def _refuse_if_coming_soon(db: Session, package_id: int, package_name: str) -> None:
    """A Coming Soon package is advertised, not sold.

    The picker shows it so members know it is on the way; both ways of taking
    it -- the free assign path and Stripe checkout -- refuse it here so the
    label cannot be bypassed by calling the API directly.
    """
    if not _has_coming_soon_column(db):
        return
    flagged = db.execute(
        text("SELECT IsComingSoon FROM SubscriptionPackage WHERE PackageID = :pid"),
        {"pid": package_id},
    ).scalar()
    if flagged:
        raise HTTPException(
            409,
            f"'{package_name}' is coming soon and cannot be selected yet.",
        )


class AssignPackageRequest(BaseModel):
    package_id: int
    billing_cycle: Optional[str] = "monthly"  # "monthly" | "yearly"


@platform_subscriptions_router.post("/assign-package/{business_id}")
def assign_package(
    business_id: int,
    req: AssignPackageRequest,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    """Assign a SubscriptionPackage to a Business without Stripe — mirrors the
    oatmeal_main admin endpoint. Only permitted in test mode so a real
    production signup can't silently skip payment; when the admin flips
    StripeTestMode off, this route refuses and the UI must run a live flow."""
    _require_business_access(db, people_id, business_id)

    pkg = db.execute(
        text("""
            SELECT PackageID, PackageName, MonthlyPrice, YearlyPrice
            FROM SubscriptionPackage
            WHERE PackageID = :pid AND IsActive = 1
        """),
        {"pid": req.package_id},
    ).mappings().fetchone()
    if not pkg:
        raise HTTPException(404, "Package not found or inactive.")

    _refuse_if_coming_soon(db, pkg["PackageID"], pkg["PackageName"])

    # A package priced at zero on both cycles costs nothing to grant, so it is
    # assigned directly in any mode -- there is no payment to skip. Anything with
    # a price still requires test mode here; the live path is /package-checkout,
    # which hands the member to Stripe.
    is_free = not float(pkg["MonthlyPrice"] or 0) and not float(pkg["YearlyPrice"] or 0)
    cfg = get_stripe_config(db)
    if not is_free and _mode_from_cfg(cfg) != "test":
        raise HTTPException(400, "Test mode is not enabled. A live payment flow is required.")

    import datetime
    days = 365 if (req.billing_cycle or "").lower() == "yearly" else 30
    start_dt = datetime.datetime.utcnow()
    end_dt = start_dt + datetime.timedelta(days=days)

    db.execute(
        text("""
            UPDATE Business
            SET SubscriptionTier = :tier,
                SubscriptionStatus = 'active',
                SubscriptionstartDate = :start,
                SubscriptionEndDate = :eod
            WHERE BusinessID = :bid
        """),
        {"tier": pkg["PackageName"], "start": start_dt, "eod": end_dt, "bid": business_id},
    )
    db.commit()
    return {
        "ok": True,
        "free": is_free,
        "test_mode": _mode_from_cfg(cfg) == "test",
        "package_id": req.package_id,
        "package_name": pkg["PackageName"],
        "billing_cycle": (req.billing_cycle or "monthly").lower(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PACKAGE CHECKOUT — Stripe Checkout for a paid SubscriptionPackage, used by the
# payment step of account setup. The SubscriptionLevels equivalent is
# /checkout above; this one bills a package and records it by name on
# Business.SubscriptionTier, which is what the plan-limit checks read.
# ─────────────────────────────────────────────────────────────────────────────

_PACKAGE_PRICE_COLUMNS = {
    ("monthly", "live"): "StripePriceIDMonthly",
    ("monthly", "test"): "StripePriceIDMonthlyTest",
    ("yearly",  "live"): "StripePriceIDYearly",
    ("yearly",  "test"): "StripePriceIDYearlyTest",
}

_PACKAGE_PRODUCT_COLUMNS = {"live": "StripeProductID", "test": "StripeProductIDTest"}

_CYCLE_INTERVAL = {"monthly": "month", "yearly": "year"}


def _ensure_package_price(db: Session, stripe, pkg: dict, cycle: str, mode: str,
                          currency: str) -> str:
    """Return a Stripe Price ID for this package/cycle, creating it if needed."""
    price_col = _PACKAGE_PRICE_COLUMNS[(cycle, mode)]
    amount = float(pkg.get("MonthlyPrice") or 0) if cycle == "monthly" else float(pkg.get("YearlyPrice") or 0)
    unit_amount = int(round(amount * 100))

    existing = pkg.get(price_col)
    if existing:
        try:
            price = stripe.Price.retrieve(existing)
            if price.unit_amount == unit_amount and price.active:
                return existing
        except Exception:
            pass

    lookup_key = f"loa_pkg_{pkg['PackageID']}_{cycle}_{unit_amount}_{currency.lower()}"

    found = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    if found.data:
        price_id = found.data[0].id
    else:
        product_col = _PACKAGE_PRODUCT_COLUMNS[mode]
        product_id = pkg.get(product_col)
        if product_id:
            try:
                stripe.Product.retrieve(product_id)
            except Exception:
                product_id = None
        if not product_id:
            product = stripe.Product.create(
                name=pkg["PackageName"],
                metadata={"package_id": str(pkg["PackageID"])},
            )
            product_id = product.id
            db.execute(
                text(f"UPDATE SubscriptionPackage SET {product_col} = :v WHERE PackageID = :pid"),
                {"v": product_id, "pid": pkg["PackageID"]},
            )
            db.commit()

        price_id = stripe.Price.create(
            product=product_id,
            currency=currency.lower(),
            unit_amount=unit_amount,
            recurring={"interval": _CYCLE_INTERVAL[cycle]},
            lookup_key=lookup_key,
            metadata={"package_id": str(pkg["PackageID"]), "billing_cycle": cycle},
        ).id

    db.execute(
        text(f"UPDATE SubscriptionPackage SET {price_col} = :v WHERE PackageID = :pid"),
        {"v": price_id, "pid": pkg["PackageID"]},
    )
    db.commit()
    return price_id


class PackageCheckoutRequest(BaseModel):
    package_id: int
    billing_cycle: Optional[str] = "monthly"
    return_url: Optional[str] = None


@platform_subscriptions_router.post("/package-checkout/{business_id}")
def start_package_checkout(
    business_id: int,
    req: PackageCheckoutRequest,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    """Create a Stripe Checkout Session for a paid package."""
    _require_business_access(db, people_id, business_id)

    cycle = (req.billing_cycle or "monthly").lower()
    if cycle not in ("monthly", "yearly"):
        raise HTTPException(400, "billing_cycle must be 'monthly' or 'yearly'.")

    cfg = get_stripe_config(db)
    mode = _mode_from_cfg(cfg)

    pkg = db.execute(
        text("SELECT * FROM SubscriptionPackage WHERE PackageID = :pid AND IsActive = 1"),
        {"pid": req.package_id},
    ).mappings().fetchone()
    if not pkg:
        raise HTTPException(404, "Package not found or inactive.")
    pkg = dict(pkg)

    _refuse_if_coming_soon(db, pkg["PackageID"], pkg["PackageName"])

    amount = float(pkg.get("MonthlyPrice") or 0) if cycle == "monthly" else float(pkg.get("YearlyPrice") or 0)
    if amount <= 0:
        raise HTTPException(
            400,
            f"'{pkg['PackageName']}' is free on the {cycle} cycle and does not need payment.",
        )

    stripe, _ = _stripe(db)

    try:
        price_id = _ensure_package_price(
            db, stripe, pkg, cycle, mode, cfg.get("CurrencyCode") or "USD")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            502,
            f"Could not set up '{pkg['PackageName']}' in Stripe: {e}. "
            "Check the Stripe key, or paste a price ID on the Subscriptions admin page.",
        )

    business = db.execute(
        text("SELECT BusinessID, BusinessName, BusinessEmail, StripeCustomerID "
             "FROM Business WHERE BusinessID = :bid"),
        {"bid": business_id},
    ).mappings().fetchone()
    if not business:
        raise HTTPException(404, "Business not found")

    customer_id = business.get("StripeCustomerID")
    if not customer_id:
        customer = stripe.Customer.create(
            email=business.get("BusinessEmail") or None,
            name=business.get("BusinessName") or None,
            metadata={"business_id": str(business_id)},
        )
        customer_id = customer.id
        db.execute(
            text("UPDATE Business SET StripeCustomerID = :cid WHERE BusinessID = :bid"),
            {"cid": customer_id, "bid": business_id},
        )
        db.commit()

    base = req.return_url if (req.return_url or "").startswith(SITE_BASE_URL) else SITE_BASE_URL
    base = base.split("?")[0].rstrip("/")

    meta = {
        "business_id": str(business_id),
        "package_id": str(pkg["PackageID"]),
        "package_name": pkg["PackageName"],
        "billing_cycle": cycle,
    }
    session = stripe.checkout.Session.create(
        mode="subscription",
        ui_mode="embedded_page",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        metadata=meta,
        subscription_data={"metadata": meta},
        return_url=f"{base}?BusinessID={business_id}&session_id={{CHECKOUT_SESSION_ID}}",
    )
    return {
        "client_secret": session.client_secret,
        "session_id": session.id,
        "publishable_key": cfg.get("StripePublishableKey"),
        "mode": mode,
    }


@platform_subscriptions_router.get("/checkout-session/{session_id}")
def package_checkout_status(
    session_id: str,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    """Status of an embedded Checkout Session, read when Stripe returns the member."""
    stripe, _ = _stripe(db)
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        raise HTTPException(404, f"Checkout session not found: {e}")

    meta = session.metadata.to_dict() if session.metadata else {}

    business_id = meta.get("business_id")
    if business_id:
        _require_business_access(db, people_id, int(business_id))

    return {
        "status": session.status,
        "payment_status": session.payment_status,
        "business_id": business_id,
        "package_name": meta.get("package_name"),
    }


@platform_subscriptions_router.post("/activate-test/{business_id}")
def activate_test_subscription(
    business_id: int,
    req: CheckoutRequest,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    """Activate a plan directly without Stripe. Only permitted when the admin
    has enabled StripeTestMode; live installs must go through /checkout."""
    _require_business_access(db, people_id, business_id)

    cfg = get_stripe_config(db)
    if _mode_from_cfg(cfg) != "test":
        raise HTTPException(400, "Test mode is not enabled. Use /checkout for live payments.")

    level = db.execute(
        text("SELECT SubscriptionID, SubscriptionTitle FROM SubscriptionLevels WHERE SubscriptionID = :id"),
        {"id": req.subscription_id},
    ).mappings().fetchone()
    if not level:
        raise HTTPException(404, "Plan not found")

    import datetime
    end_dt = datetime.datetime.utcnow() + datetime.timedelta(days=30)
    db.execute(
        text("""
            UPDATE Business
            SET SubscriptionLevel = :lvl,
                SubscriptionStatus = 'active',
                SubscriptionstartDate = COALESCE(SubscriptionstartDate, GETDATE()),
                SubscriptionEndDate = :eod
            WHERE BusinessID = :bid
        """),
        {"lvl": req.subscription_id, "eod": end_dt, "bid": business_id},
    )
    db.commit()
    return {"ok": True, "test_mode": True, "subscription_level_id": req.subscription_id}


@platform_subscriptions_router.post("/portal/{business_id}")
def customer_portal(
    business_id: int,
    db: Session = Depends(get_db),
    people_id: str = Depends(get_current_user),
):
    _require_business_access(db, people_id, business_id)
    stripe, _ = _stripe(db)
    row = db.execute(
        text("SELECT StripeCustomerID FROM Business WHERE BusinessID = :bid"),
        {"bid": business_id},
    ).fetchone()
    if not row or not row[0]:
        raise HTTPException(400, "No Stripe customer yet — start a subscription first.")
    session = stripe.billing_portal.Session.create(
        customer=row[0],
        return_url=f"{OFN_BASE_URL}/account/subscription?BusinessID={business_id}",
    )
    return {"portal_url": session.url}


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

@platform_subscriptions_router.post("/webhook")
async def subscription_webhook(request: Request):
    """Handle Stripe subscription lifecycle. Uses its own webhook secret stored
    in OFNPlatformSettings.PlatformSubscriptionWebhookSecret; falls back to
    StripeWebhookSecret if the platform-subscription secret isn't set."""
    with SessionLocal() as db:
        cfg = get_stripe_config(db)
    import stripe
    stripe.api_key = cfg.get("StripeSecretKey") or ""
    secret = cfg.get("PlatformSubscriptionWebhookSecret") or cfg.get("StripeWebhookSecret") or ""

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret) if secret else json.loads(payload)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid webhook signature")

    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}

    with SessionLocal() as db:
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata", {}) or {}
            business_id = metadata.get("business_id")
            level_id = metadata.get("subscription_level_id")
            package_name = metadata.get("package_name")
            subscription_id = obj.get("subscription")
            customer_id = obj.get("customer")
            if business_id and subscription_id:
                db.execute(
                    text("""
                        UPDATE Business
                        SET StripeCustomerID = COALESCE(:cid, StripeCustomerID),
                            StripeSubscriptionID = :sid,
                            SubscriptionLevel = COALESCE(:lvl, SubscriptionLevel),
                            SubscriptionTier = COALESCE(:tier, SubscriptionTier),
                            SubscriptionStatus = 'active',
                            SubscriptionstartDate = COALESCE(SubscriptionstartDate, GETDATE())
                        WHERE BusinessID = :bid
                    """),
                    {"cid": customer_id, "sid": subscription_id,
                     "lvl": int(level_id) if level_id else None,
                     "tier": package_name,
                     "bid": int(business_id)},
                )
                db.commit()

        elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
            metadata = obj.get("metadata", {}) or {}
            business_id = metadata.get("business_id")
            subscription_id = obj.get("id")
            status = obj.get("status")
            current_period_end = obj.get("current_period_end")
            if not business_id and subscription_id:
                row = db.execute(
                    text("SELECT BusinessID FROM Business WHERE StripeSubscriptionID = :sid"),
                    {"sid": subscription_id},
                ).fetchone()
                if row:
                    business_id = row[0]
            if business_id:
                import datetime
                end_dt = datetime.datetime.utcfromtimestamp(current_period_end) if current_period_end else None
                db.execute(
                    text("""
                        UPDATE Business
                        SET SubscriptionStatus = :st,
                            SubscriptionEndDate = :eod,
                            StripeSubscriptionID = COALESCE(:sid, StripeSubscriptionID)
                        WHERE BusinessID = :bid
                    """),
                    {"st": status, "eod": end_dt, "sid": subscription_id, "bid": int(business_id)},
                )
                db.commit()

        elif event_type == "customer.subscription.deleted":
            subscription_id = obj.get("id")
            db.execute(
                text("""
                    UPDATE Business
                    SET SubscriptionStatus = 'cancelled'
                    WHERE StripeSubscriptionID = :sid
                """),
                {"sid": subscription_id},
            )
            db.commit()

        elif event_type == "invoice.payment_failed":
            subscription_id = obj.get("subscription")
            if subscription_id:
                db.execute(
                    text("""
                        UPDATE Business SET SubscriptionStatus = 'past_due'
                        WHERE StripeSubscriptionID = :sid AND SubscriptionStatus != 'cancelled'
                    """),
                    {"sid": subscription_id},
                )
                db.commit()

    return {"received": True}


# ─────────────────────────────────────────────────────────────────────────────
# PACKAGE ADMIN — manage plans from the site instead of by hand in Stripe.
#
# The database is the source of truth. Checkout reads MonthlyPrice/YearlyPrice
# straight from SubscriptionPackage and syncs Stripe itself: _ensure_package_price
# creates the product and price on first use, and because a stored price whose
# amount no longer matches is replaced rather than reused, changing a price here
# is enough — Stripe follows on the next checkout. Stripe prices are immutable,
# so the old one simply stops being used; subscriptions already billing on it
# keep their agreed rate until they are changed deliberately.
# ─────────────────────────────────────────────────────────────────────────────

_EDITABLE_PACKAGE_COLUMNS = {
    "Description":          str,
    "MonthlyPrice":         float,
    "YearlyPrice":          float,
    "SortOrder":            int,
    "IsActive":             bool,
    "IsSelfService":        bool,
    "IsComingSoon":         bool,
    "MaxForSaleListings":   int,
    "MaxStudListings":      int,
    "MaxDirectoryListings": int,
    "MaxEquipmentListings": int,
    "MaxJobPostings":       int,
    "MaxServiceListings":   int,
}


def _package_payload(db: Session, row: dict) -> dict:
    """One package plus the feature keys it turns on."""
    features = [
        r[0] for r in db.execute(text("""
            SELECT csm.FeatureKey
            FROM SubscriptionPackageFeature spf
            JOIN CompanySiteManagement csm ON csm.FeatureID = spf.FeatureID
            WHERE spf.PackageID = :pid
            ORDER BY csm.SortOrder
        """), {"pid": row["PackageID"]}).fetchall()
    ]
    out = {k: row.get(k) for k in ("PackageID", "PackageName", "Description",
                                   "BusinessTypeID", "SortOrder")}
    for k in ("MonthlyPrice", "YearlyPrice"):
        out[k] = float(row[k]) if row.get(k) is not None else None
    for k in ("IsActive", "IsSelfService", "IsComingSoon"):
        out[k] = bool(row.get(k))
    for k in _EDITABLE_PACKAGE_COLUMNS:
        if k.startswith("Max"):
            out[k] = row.get(k)          # None means unlimited
    out["features"] = features
    return out


@platform_subscriptions_router.get("/admin/packages")
def admin_list_packages(
    people_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every package with its allowances and features, plus the feature catalog."""
    if not _is_admin(people_id):
        raise HTTPException(403, "Platform admin only")

    has_self_service = _has_self_service_column(db)
    cols = "*"
    rows = db.execute(text(f"SELECT {cols} FROM SubscriptionPackage ORDER BY SortOrder, PackageName")).mappings().fetchall()
    catalog = [
        {"feature_key": r[0], "feature_name": r[1]}
        for r in db.execute(text(
            "SELECT FeatureKey, FeatureName FROM CompanySiteManagement ORDER BY SortOrder"
        )).fetchall()
    ]
    return {
        "packages": [_package_payload(db, dict(r)) for r in rows],
        "features": catalog,
        "self_service_supported": has_self_service,
    }


@platform_subscriptions_router.put("/admin/packages/{package_id}")
def admin_update_package(
    package_id: int,
    payload: dict,
    people_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a package's prices, allowances and features.

    PackageName is deliberately not editable: businesses are linked to their
    package by that string (Business.SubscriptionTier), so renaming here would
    silently detach every subscriber.

    An allowance sent as null means unlimited; 0 excludes that listing type.
    """
    if not _is_admin(people_id):
        raise HTTPException(403, "Platform admin only")

    row = db.execute(
        text("SELECT * FROM SubscriptionPackage WHERE PackageID = :pid"),
        {"pid": package_id},
    ).mappings().fetchone()
    if not row:
        raise HTTPException(404, "Package not found")

    sets, params = [], {"pid": package_id}
    for col, kind in _EDITABLE_PACKAGE_COLUMNS.items():
        if col not in payload:
            continue
        if col == "IsSelfService" and not _has_self_service_column(db):
            continue
        if col == "IsComingSoon" and not _has_coming_soon_column(db):
            continue
        value = payload[col]
        if value is not None:
            try:
                value = kind(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{col} must be a {kind.__name__}")
            if col.startswith("Max") and value < 0:
                raise HTTPException(400, f"{col} cannot be negative")
            if col.endswith("Price") and value < 0:
                raise HTTPException(400, f"{col} cannot be negative")
        elif col in ("Description",):
            value = ""
        sets.append(f"{col} = :{col}")
        params[col] = value

    if sets:
        db.execute(text(
            f"UPDATE SubscriptionPackage SET {', '.join(sets)}, UpdatedAt = GETDATE() "
            "WHERE PackageID = :pid"
        ), params)

    if isinstance(payload.get("features"), list):
        keys = [str(k) for k in payload["features"]]
        db.execute(text("DELETE FROM SubscriptionPackageFeature WHERE PackageID = :pid"),
                   {"pid": package_id})
        if keys:
            db.execute(text("""
                INSERT INTO SubscriptionPackageFeature (PackageID, FeatureID)
                SELECT :pid, FeatureID FROM CompanySiteManagement
                WHERE FeatureKey IN :keys
            """).bindparams(bindparam("keys", expanding=True)),
                {"pid": package_id, "keys": keys})

    db.commit()
    fresh = db.execute(
        text("SELECT * FROM SubscriptionPackage WHERE PackageID = :pid"),
        {"pid": package_id},
    ).mappings().fetchone()
    return _package_payload(db, dict(fresh))

"""
Standalone Livestock of America (LOA) API entrypoint.

Serves breed KB, livestock marketplace, ranches, animals, herd health, and auth
for the LOA frontend Cloud Run site, plus the workspace features the LOA
frontend ported from OFN (website builder, events, blog, accounting, HR, ...).

Run locally:
    uvicorn livestock.api:app --reload --port 8000

On Cloud Run the Dockerfile CMD does the same without --reload.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

# Ensure the repo root is on sys.path so `from app.routers …` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv()

from app.routers import (  # noqa: E402
    animals, auth, businesses, business_photos, herd_health, livestock, ranches, services,
)
from app.routers.marketplace import marketplace_router  # noqa: E402
from app.database import get_db  # noqa: E402
from app.routers.platform_subscriptions import platform_subscriptions_router  # noqa: E402

# Workspace features the LOA frontend ported from OFN (website builder, events,
# blog, accounting, HR, ...). Mounted exactly as app/main.py mounts them.
from app.routers import (  # noqa: E402
    associations, ingredient_knowledgebase, produce, processed_food, meat, mill,
    job_board, land_leasing, supplier_directory, weather, website_builder,
    website_ai, sfproducts, event_features, events, event_fiber_arts, event_fleece,
    event_spinoff, event_halter, event_auction, event_vendor_fair, event_dining,
    event_farm_tour, event_simple, event_conference, event_competition,
    event_checkin, event_broadcast, my_registrations, event_analytics,
    company_features, blog, accounting, event_registration_cart, event_meals,
    event_exports, event_mailing_list, event_promo_codes, event_waitlist,
    event_testimonials, event_sponsorship, event_leads, event_floor_plan,
    event_booth_services, event_coi, esg_reports, stripe_payments, news,
    commodity_history, farmer_settlement, hr, farm_infrastructure, farm_kpi,
    supplier_scorecard, scale_tickets, cash_flow, reports, farm_pl, document_vault,
    farm_safety, buyer_crm, compliance_audit, price_list, delivery_routes, rbac,
)
from app.routers.equipment_marketplace import equipment_router  # noqa: E402
from app.routers.food_wanted import food_wanted_router  # noqa: E402


def _cors_origins(*env_values: str) -> list[str]:
    """Build allow_origins from local defaults + comma-separated FRONTEND_URL env values."""
    origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
    ]
    for raw in env_values:
        if not raw:
            continue
        for part in raw.split(","):
            origin = part.strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
    return origins


FRONTEND_URL = os.getenv("FRONTEND_URL", "")
LOA_FRONTEND_URL = os.getenv("LOA_FRONTEND_URL", "")
ALLOWED_ORIGINS = _cors_origins(FRONTEND_URL, LOA_FRONTEND_URL)

app = FastAPI(title="Livestock of America API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Breed / species knowledge base
app.include_router(livestock.router)
# Livestock marketplace (for-sale, studs, animal detail, filters)
app.include_router(marketplace_router, prefix="/api/marketplace")
# Ranch directory
app.include_router(ranches.router)
# Business directory + photo pages
app.include_router(businesses.router)
app.include_router(business_photos.router)
app.include_router(services.router)
# Animal CRUD (seller / herd manager)
app.include_router(animals.router)
# Herd health
app.include_router(herd_health.router)
# Auth (same JWT SECRET_KEY as main backend when secrets match)
app.include_router(auth.router)
# Package listing limits / Stripe package checkout
app.include_router(platform_subscriptions_router)

# ── LOA workspace features (same order and prefixes as app/main.py) ──
app.include_router(associations.router)
app.include_router(ingredient_knowledgebase.router)
app.include_router(produce.router)
app.include_router(processed_food.router)
app.include_router(meat.router)
app.include_router(equipment_router, prefix="/api/equipment")
app.include_router(food_wanted_router, prefix="/api/food-wanted")
app.include_router(mill.router)
app.include_router(job_board.router)
app.include_router(land_leasing.router)
app.include_router(supplier_directory.router)
app.include_router(weather.router)
app.include_router(website_builder.router)
app.include_router(website_ai.router)
app.include_router(sfproducts.router)
app.include_router(event_features.router)
app.include_router(events.router)
app.include_router(event_fiber_arts.router)
app.include_router(event_fleece.router)
app.include_router(event_spinoff.router)
app.include_router(event_halter.router)
app.include_router(event_auction.router)
app.include_router(event_vendor_fair.router)
app.include_router(event_dining.router)
app.include_router(event_farm_tour.router)
app.include_router(event_simple.router)
app.include_router(event_conference.router)
app.include_router(event_competition.router)
app.include_router(event_checkin.router)
app.include_router(event_broadcast.router)
app.include_router(my_registrations.router)
app.include_router(event_analytics.router)
app.include_router(company_features.router)
app.include_router(blog.router)
app.include_router(accounting.router)
app.include_router(event_registration_cart.router)
app.include_router(event_meals.router)
app.include_router(event_exports.router)
app.include_router(event_mailing_list.router)
app.include_router(event_promo_codes.router)
app.include_router(event_waitlist.router)
app.include_router(event_testimonials.router)
app.include_router(event_sponsorship.router)
app.include_router(event_leads.router)
app.include_router(event_floor_plan.router)
app.include_router(event_booth_services.router)
app.include_router(event_coi.router)
app.include_router(esg_reports.router)
app.include_router(stripe_payments.router)
app.include_router(news.router)
app.include_router(commodity_history.router)
app.include_router(farmer_settlement.router)
app.include_router(hr.router)
app.include_router(farm_infrastructure.router)
app.include_router(farm_kpi.router)
app.include_router(supplier_scorecard.router)
app.include_router(scale_tickets.router)
app.include_router(cash_flow.router)
app.include_router(reports.router)
app.include_router(farm_pl.router)
app.include_router(document_vault.router)
app.include_router(farm_safety.router)
app.include_router(buyer_crm.router)
app.include_router(compliance_audit.router)
app.include_router(price_list.router)
app.include_router(delivery_routes.router)
app.include_router(rbac.router)
app.include_router(rbac.AUDIT_ROUTER)


# Public testimonials for website blocks. Inline in app/main.py, so copied here.
@app.get("/api/testimonials")
def get_public_testimonials(BusinessID: int, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT TestimonialsID, CustomerName AS AuthorName,
               Testimonial AS Content, Rating,
               City, State, Organization, URL AS Website,
               TestimonialDate, PeopleID, Name,
               AnimalID, AnimalName, TestimonialsType
        FROM Testimonials
        WHERE CustID = :bid
        ORDER BY testimonialsOrder, TestimonialsID DESC
    """), {"bid": BusinessID}).fetchall()
    return [dict(r._mapping) for r in rows]


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "livestock"}

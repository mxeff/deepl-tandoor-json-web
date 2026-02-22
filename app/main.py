import os, json, re
from pathlib import Path

import deepl
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from ingredient_parser import parse_ingredient
from pint import UnitRegistry
from recipe_scrapers import scrape_me

app = FastAPI()
templates = Jinja2Templates(directory=Path("app/templates"))
ureg = UnitRegistry()

translator = deepl.Translator(os.getenv("DEEPL_API_KEY"))


def get_text(field) -> str:
    if field is None:
        return ""

    if isinstance(field, list):
        return " ".join(getattr(i, "text", str(i)) for i in field)

    return getattr(field, "text", str(field))


def translate_batch(texts: list[str]) -> list[str]:
    if not texts:
        return []

    results = translator.translate_text(
        texts,
        source_lang="EN",
        target_lang="DE",
        formality="less",
        context="This text is from a cookbook about cooking or baking.",
        custom_instructions=[
            "Convert imperial measurements to metric units except cups.",
        ],
    )

    return [r.text for r in results]


def extract_amount_unit(parsed) -> tuple[float, str]:
    if not parsed.amount:
        return 0.0, ""

    a = parsed.amount[0]

    try:
        amount = float(a.quantity)
    except (ValueError, TypeError):
        amount = 0.0

    if not a.unit:
        return amount, ""

    unit = str(a.unit)

    if unit:
        try:
            q = ureg.Quantity(amount, unit).to(unit)
            amount = round(float(q.magnitude), 1)
        except Exception:
            pass

    return amount, unit


def strip_non_alpha(s: str) -> str:
    return re.sub(r"^[^a-zA-ZäöüÄÖÜß]+|[^a-zA-ZäöüÄÖÜß]+$", "", s.strip())


def extract_tagged_food_unit(text: str) -> tuple[str, str]:
    match = re.search(r"<x>(.*?)</x>\s*(.*)", text)

    if match:
        return strip_non_alpha(match.group(2)), strip_non_alpha(match.group(1))

    return strip_non_alpha(text), ""


def minutes_to_iso(minutes) -> str:
    if not minutes:
        return ""

    return f"PT{int(minutes)}M"


async def process_recipe(data: dict) -> dict:
    raw_steps = data.get("instructions_list") or [
        s.strip() for s in (data.get("instructions") or "").split("\n") if s.strip()
    ]

    raw_category = data.get("category", "") or ""
    raw_yield = data.get("yields", "") or ""

    raw_keywords = data.get("keywords") or []

    if isinstance(raw_keywords, str):
        raw_keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]

    raw_ingredients = []

    for ingredient in data.get("ingredients") or []:
        parsed = parse_ingredient(ingredient)

        amount, unit = extract_amount_unit(parsed)

        amount = amount if amount > 0 else ""

        note = ", ".join(
            filter(
                None,
                [
                    get_text(parsed.size),
                    get_text(parsed.preparation),
                    get_text(parsed.comment),
                ],
            )
        )

        note = f", {note}" if note else ""

        raw_ingredients.append(
            f"{amount} {unit} {get_text(parsed.name)} {note}".strip()
        )

    batch = (
        [data.get("title", ""), data.get("description", ""), raw_category, raw_yield]
        + raw_ingredients
        + raw_steps
        + raw_keywords
    )

    translated = translate_batch(batch)

    title = translated[0]
    description = translated[1]
    category = translated[2]
    yields = translated[3]
    offset = 4

    ingredients = []

    for ingredient in translated[offset : offset + len(raw_ingredients)]:
        ingredients.append(ingredient.strip())

    offset += len(raw_ingredients)

    steps = [
        {"@type": "HowToStep", "text": translated[offset + i]}
        for i in range(len(raw_steps))
    ]

    offset += len(raw_steps)

    keywords_de = translated[offset : offset + len(raw_keywords)]

    offset += len(raw_keywords)

    return {
        "name": title,
        "description": description,
        "ingredients": ingredients,
        "steps": steps,
        "keywords": keywords_de,
        "category": category,
        "yield": yields,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/import", response_class=HTMLResponse)
async def import_recipe(request: Request, url: str = Form(...)):
    try:
        scraper = scrape_me(url)
        data = scraper.to_json()

        processed = await process_recipe(data)

        recipe = {
            "@context": "http://schema.org",
            "@type": "Recipe",
            "name": processed["name"],
            "description": processed["description"],
            "image": data.get("image", ""),
            "prepTime": minutes_to_iso(data.get("prep_time")),
            "cookTime": minutes_to_iso(data.get("cook_time")),
            "totalTime": minutes_to_iso(data.get("total_time")),
            "recipeYield": processed["yield"],
            "recipeCategory": processed["category"],
            "keywords": processed["keywords"],
            "recipeIngredient": processed["ingredients"],
            "recipeInstructions": processed["steps"],
        }

        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "recipe": recipe,
                "recipe_json": json.dumps(recipe, ensure_ascii=False, indent=2),
                "original_url": url,
            },
        )

    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": str(e),
            },
        )

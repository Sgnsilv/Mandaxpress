"""Rotas de gerenciamento e consulta de produtos: /api/products/*"""
from typing import Optional

from flask import Blueprint, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...domain.models import Product, Role
from .errors import RequestValidationError
from .security import login_required

bp = Blueprint("products", __name__, url_prefix="/api/products")


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default="Geral", max_length=80)
    price: str = Field(min_length=1, max_length=20)
    is_active: bool = True


class UpdateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default="Geral", max_length=80)
    price: str = Field(min_length=1, max_length=20)


class ProductStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RequestValidationError([{"field": "body", "message": "Corpo JSON obrigatório."}])
    return data


def _serialize_product(p: Product) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "category": p.category,
        "price": f"{p.price:.2f}",
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _parse_model(model_cls):
    try:
        return model_cls.model_validate(_json_body())
    except ValidationError as exc:
        raise RequestValidationError(
            [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
             for e in exc.errors()]
        ) from exc


@bp.get("")
@login_required()
def list_products():
    active_only_param = request.args.get("active_only", "").lower()
    active_only = active_only_param in ("1", "true", "yes")
    search = request.args.get("q") or request.args.get("search")
    category = request.args.get("category")

    container = current_app.extensions["mx_container"]
    items = container.products.list_products(
        active_only=active_only,
        search=search,
        category=category,
    )
    return jsonify({"products": [_serialize_product(p) for p in items]})


@bp.post("")
@login_required(Role.ADMIN)
def create_product():
    data = _parse_model(CreateProductRequest)
    container = current_app.extensions["mx_container"]
    product = container.products.create_product(
        actor=g.auth.user,
        name=data.name,
        price=data.price,
        category=data.category,
        is_active=data.is_active,
    )
    return jsonify({"product": _serialize_product(product)}), 201


@bp.put("/<int:product_id>")
@login_required(Role.ADMIN)
def update_product(product_id: int):
    data = _parse_model(UpdateProductRequest)
    container = current_app.extensions["mx_container"]
    product = container.products.update_product(
        actor=g.auth.user,
        product_id=product_id,
        name=data.name,
        price=data.price,
        category=data.category,
    )
    return jsonify({"product": _serialize_product(product)})


@bp.patch("/<int:product_id>/status")
@login_required(Role.ADMIN)
def set_product_status(product_id: int):
    data = _parse_model(ProductStatusRequest)
    container = current_app.extensions["mx_container"]
    product = container.products.set_product_active(
        actor=g.auth.user,
        product_id=product_id,
        is_active=data.is_active,
    )
    return jsonify({"product": _serialize_product(product)})

"""Authenticated, frozen-menu meal rounds. All mutations use the parent lock.

The source menu and photos are copied assets; no old order, token, or service is
accessed. Internal tables intentionally have no native Desk/REST permissions.
"""
import csv
import hashlib
import json
import re
from contextlib import contextmanager
from io import BytesIO, StringIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from research_group.ai.security import global_lock
from research_group.policy import DRAFT, PUBLISHED, ROLES, SECRETARY, deny
from research_group.surveys import _require_current_roles

CLOSED, ACTIVE, VOID = "已关闭", "有效", "已作废"
MEAL_DOCTYPES = ("RG Meal Round", "RG Meal Item", "RG Meal Order", "RG Meal Order Item")
MAX_ROUNDS, MAX_ORDERS, BUDGET_CENTS = 50, 5000, 5000
_TOKEN = object()
_MENU_PATH = Path(__file__).parent / "public" / "menu" / "default-menu.json"
_ITEM_IDS = tuple(f"meal-{i}" for i in range(1, 8)) + tuple(f"addon-{i}" for i in range(1, 8))


def _text(value, label, maximum, required=False, multiline=False):
    if value is None:
        value = ""
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 and (not multiline or char not in "\n\r\t") for char in value)
            or (required and not value.strip())):
        frappe.throw(f"{label}须为有效文本，最多 {maximum} 字。")
    return value.strip()


def _json(value, label, maximum=65536):
    if not isinstance(value, str):
        return value
    if len(value.encode("utf-8")) > maximum:
        frappe.throw(f"{label}过大。")
    try:
        return json.loads(value, object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        frappe.throw(f"{label}须为有效 JSON。")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            frappe.throw("JSON 字段不能重复。")
        result[key] = value
    return result


def _integer(value, label, minimum=0, maximum=2_000_000_000, form=False):
    if form and isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,9}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        frappe.throw(f"{label}须为 {minimum} 至 {maximum} 的整数。")
    return value


def _boolean(value):
    # Frappe form encoding serializes booleans as their JSON spelling.
    if value in ("true", "false"):
        return value == "true"
    if type(value) is not bool:
        frappe.throw("确认状态须为布尔值。")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deadline(value):
    if value in (None, ""):
        return None
    try:
        parsed = get_datetime(value)
        if parsed.tzinfo is not None:
            raise ValueError
        return parsed
    except (TypeError, ValueError, OverflowError):
        frappe.throw("截止时间须为 Asia/Shanghai 的有效日期时间。")


def _image(item_id):
    return f"/assets/research_group/menu/2026-11-07/{item_id}.jpg" if item_id.startswith("meal-") else ""


def validate_menu(menu):
    """Normalize the current seven-main/seven-extra template, never prices from orders."""
    menu = _json(menu, "菜单")
    if (not isinstance(menu, dict) or set(menu) - {"version", "mode", "budgetCents", "items",
                                               "mainDishes", "sideDishes", "fixedExtras"}
            or menu.get("mode", "priced") != "priced" or menu.get("budgetCents", BUDGET_CENTS) != BUDGET_CENTS
            or type(menu.get("budgetCents", BUDGET_CENTS)) is not int):
        frappe.throw("工作餐须使用 50 元预算的计价菜单。")
    source = menu.get("items")
    if not isinstance(source, list) or len(source) != 14:
        frappe.throw("菜单须保留 7 份主餐和 7 种加料。")
    items, seen = [], set()
    for item in source:
        if not isinstance(item, dict) or set(item) - {
            "id", "category", "name", "priceCents", "spice", "description", "staples", "fixedStaple", "imageUrl",
        }:
            frappe.throw("菜单含未知字段。")
        item_id = item.get("id")
        if item_id not in _ITEM_IDS or item_id in seen:
            frappe.throw("餐品标识无效或重复。")
        seen.add(item_id)
        category = "meal" if item_id.startswith("meal-") else "addon"
        if item.get("category") != category:
            frappe.throw("主餐与加料分类不可互换。")
        row = {"id": item_id, "category": category,
               "name": _text(item.get("name"), "餐名", 200, required=True),
               "priceCents": _integer(item.get("priceCents"), "单价（分）", 1, 100000),
               "description": _text(item.get("description"), "配菜说明", 2000, required=True, multiline=True),
               "imageUrl": _image(item_id)}
        spice = item.get("spice", 0)
        if type(spice) not in (int, float) or not 0 <= spice <= 5:
            frappe.throw("辣度须在 0 至 5 之间。")
        row["spice"] = spice
        choices, fixed = item.get("staples", []), item.get("fixedStaple", "")
        if not isinstance(choices, list) or len(choices) > 8:
            frappe.throw("主食选项无效。")
        choices = [_text(choice, "主食", 80, required=True) for choice in choices]
        if len(set(choices)) != len(choices):
            frappe.throw("主食选项不能重复。")
        fixed = _text(fixed, "固定主食", 80)
        if category == "meal":
            if bool(choices) == bool(fixed):
                frappe.throw("每份主餐须指定可选主食或固定主食。")
            if choices:
                row["staples"] = choices
            else:
                row["fixedStaple"] = fixed
        elif choices or fixed:
            frappe.throw("加料不能设置主食。")
        items.append(row)
    items.sort(key=lambda row: _ITEM_IDS.index(row["id"]))
    return {"version": _text(menu.get("version"), "菜单版本", 80, required=True),
            "mode": "priced", "budgetCents": BUDGET_CENTS, "items": items}


@frappe.whitelist()
def default_menu():
    _guard()
    return validate_menu(json.loads(_MENU_PATH.read_text(encoding="utf-8")))


@contextmanager
def _atomic():
    key = "rg_meal_" + frappe.generate_hash(length=12)
    frappe.db.savepoint(key)
    try:
        yield
    except Exception:
        frappe.db.rollback(save_point=key)
        raise


def _save(doc, new=False):
    doc.flags.rg_meal = _TOKEN
    with _atomic():
        return doc.insert(ignore_permissions=True) if new else doc.save(ignore_permissions=True)


def _guard(allowed=ROLES, participant=False):
    # Native User/Role/Member saves take this gate first. Keep the same order.
    global_lock()
    return _require_current_roles(allowed, participant=participant)


def _children(doc, doctype, fieldname):
    rows = frappe.db.sql(
        f"SELECT * FROM `tab{doctype}` WHERE parent=%s AND parenttype=%s AND parentfield=%s "
        "ORDER BY idx FOR UPDATE", (doc.name, doc.doctype, fieldname), as_dict=True,
    )
    doc.set(fieldname, rows)


def _locked_round(name, allowed=ROLES, participant=False):
    _text(name, "轮次", 140, required=True)
    assigned = _guard(allowed, participant=participant)
    try:
        doc = frappe.get_doc("RG Meal Round", name, for_update=True)
    except frappe.DoesNotExistError:
        deny()
    assigned = _require_current_roles(allowed, participant=participant)
    if doc.status == DRAFT and SECRETARY not in assigned:
        deny()
    _children(doc, "RG Meal Item", "items")
    return doc


def _revision(doc, value):
    value = _integer(value, "轮次/订单版本", form=True)
    if value != int(doc.revision or 0):
        frappe.throw("内容已有更新，请刷新后核对，原记录未更改。", frappe.TimestampMismatchError)


def _menu(doc):
    return json.loads(doc.menu_snapshot) if doc.status != DRAFT else _menu_from_items(doc)


def _menu_from_items(doc):
    items = []
    for row in doc.get("items") or []:
        item = {"id": row.item_id, "category": row.category, "name": row.item_name,
                "priceCents": row.price_cents, "spice": float(row.spice or 0),
                "description": row.description or ""}
        if row.staples_json:
            item["staples"] = json.loads(row.staples_json)
        if row.fixed_staple:
            item["fixedStaple"] = row.fixed_staple
        items.append(item)
    return validate_menu({"version": doc.menu_version, "items": items})


def _set_menu(doc, menu):
    normalized = validate_menu(menu)
    doc.menu_version = normalized["version"]
    doc.set("items", [])
    for row in normalized["items"]:
        doc.append("items", {"item_id": row["id"], "category": row["category"], "item_name": row["name"],
                             "price_cents": row["priceCents"], "spice": row["spice"],
                             "description": row["description"], "staples_json": _canonical(row.get("staples", [])),
                             "fixed_staple": row.get("fixedStaple", ""), "image_url": row["imageUrl"]})


def _is_open(doc, current=None):
    return doc.status == PUBLISHED and (not doc.deadline or (current or now_datetime()) < get_datetime(doc.deadline))


def _round_dict(doc, include_menu=True):
    current = now_datetime()
    result = {key: doc.get(key) for key in (
        "name", "title", "meeting", "status", "deadline", "revision", "publisher", "published_at", "closed_at",
    )}
    result.update(server_time=current, accepting_orders=bool(_is_open(doc, current)), budget_cents=BUDGET_CENTS)
    if include_menu:
        result["menu"] = _menu(doc)
        result["menu_sha256"] = doc.menu_sha256 or ""
    return result


def _own_order(doc):
    rows = frappe.db.sql("SELECT name FROM `tabRG Meal Order` WHERE `round`=%s AND member=%s FOR UPDATE",
                         (doc.name, frappe.session.user))
    return _load_order(rows[0][0]) if rows else None


def _load_order(name):
    doc = frappe.get_doc("RG Meal Order", name, for_update=True)
    _children(doc, "RG Meal Order Item", "items")
    return doc


def _order_dict(doc):
    if doc is None:
        return None
    result = {key: doc.get(key) for key in (
        "name", "round", "member", "member_name", "remarks", "total_cents", "over_budget", "status", "revision",
        "submitted_at", "updated_at", "budget_acknowledged_at", "voided_by", "voided_at", "void_reason",
    )}
    result["items"] = [{"id": row.item_id, "category": row.category, "name": row.item_name,
                        "priceCents": row.price_cents, "quantity": row.quantity, "staple": row.staple or "",
                        "subtotalCents": row.subtotal_cents} for row in doc.items]
    return result


@frappe.whitelist()
def list_rounds(limit=50):
    assigned = _guard()
    limit = _integer(limit, "页长", 1, 100, form=True)
    condition = "" if SECRETARY in assigned else "WHERE status != %s"
    values = () if not condition else (DRAFT,)
    rows = frappe.db.sql(f"SELECT name FROM `tabRG Meal Round` {condition} "
                         f"ORDER BY creation DESC, name DESC LIMIT {limit} FOR UPDATE", values)
    return [_round_dict(_locked_round(row[0]), include_menu=False) for row in rows]


@frappe.whitelist()
def get_round(name):
    doc = _locked_round(name)
    result = _round_dict(doc)
    result["my_order"] = _order_dict(_own_order(doc))
    result["can_manage"] = SECRETARY in _require_current_roles(ROLES)
    return result


@frappe.whitelist(methods=["POST"])
def create_round(title, deadline=None, meeting=None, menu=None, copy_from=None):
    _guard((SECRETARY,))
    if menu is not None and copy_from:
        frappe.throw("请使用菜单或复制来源之一。")
    if copy_from:
        menu = _menu(_locked_round(copy_from, (SECRETARY,)))
    if menu is None:
        menu = json.loads(_MENU_PATH.read_text(encoding="utf-8"))
    doc = frappe.get_doc({"doctype": "RG Meal Round", "title": _text(title, "标题", 200, required=True),
                          "deadline": _deadline(deadline), "meeting": _meeting(meeting), "status": DRAFT,
                          "publisher": frappe.session.user, "revision": 1})
    _set_menu(doc, menu)
    _save(doc, new=True)
    return _round_dict(doc)


def _meeting(name):
    name = _text(name, "关联组会", 140)
    if name:
        from research_group.policy import secure_get
        secure_get("RG Group Meeting", name)
    return name or None


@frappe.whitelist(methods=["POST"])
def update_draft(name, expected_revision, title=None, deadline=None, meeting=None, menu=None):
    doc = _locked_round(name, (SECRETARY,))
    _revision(doc, expected_revision)
    if doc.status != DRAFT:
        frappe.throw("菜单发布后冻结，只能复制为新轮次。")
    if title is not None:
        doc.title = _text(title, "标题", 200, required=True)
    if deadline is not None:
        doc.deadline = _deadline(deadline)
    if meeting is not None:
        doc.meeting = _meeting(meeting)
    if menu is not None:
        _set_menu(doc, menu)
    doc.revision += 1
    _save(doc)
    return _round_dict(doc)


@frappe.whitelist(methods=["POST"])
def publish_round(name, expected_revision):
    doc = _locked_round(name, (SECRETARY,))
    _revision(doc, expected_revision)
    if doc.status != DRAFT:
        frappe.throw("只有草稿可以发布。")
    current = now_datetime()
    if doc.deadline and get_datetime(doc.deadline) <= current:
        frappe.throw("截止时间必须晚于服务器当前时间。")
    doc.menu_snapshot = _canonical(_menu_from_items(doc))
    doc.menu_sha256 = hashlib.sha256(doc.menu_snapshot.encode("utf-8")).hexdigest()
    doc.status, doc.published_at, doc.revision = PUBLISHED, current, int(doc.revision) + 1
    _save(doc)
    return _round_dict(doc)


@frappe.whitelist(methods=["POST"])
def set_round_status(name, closed, expected_revision):
    doc = _locked_round(name, (SECRETARY,))
    _revision(doc, expected_revision)
    closed = _boolean(closed)
    if doc.status not in (PUBLISHED, CLOSED):
        frappe.throw("请先发布轮次。")
    target = CLOSED if closed else PUBLISHED
    if target == doc.status:
        return _round_dict(doc)
    if not closed and doc.deadline and get_datetime(doc.deadline) <= now_datetime():
        frappe.throw("截止时间已过，重新开放不能绕过截止时间。")
    doc.status, doc.closed_at, doc.revision = target, now_datetime() if closed else None, int(doc.revision) + 1
    _save(doc)
    return _round_dict(doc)


@frappe.whitelist(methods=["POST"])
def set_deadline(name, deadline, expected_revision):
    doc = _locked_round(name, (SECRETARY,))
    _revision(doc, expected_revision)
    doc.deadline, doc.revision = _deadline(deadline), int(doc.revision) + 1
    _save(doc)
    return _round_dict(doc)


def canonical_order(menu, items, remarks="", budget_acknowledged=False):
    menu = validate_menu(menu)
    items = _json(items, "订单")
    if not isinstance(items, list) or not 1 <= len(items) <= 14:
        frappe.throw("请选择 1 至 14 种餐品，至少一份主餐。")
    by_id, result, seen, total, main_count = {row["id"]: row for row in menu["items"]}, [], set(), 0, 0
    for raw in items:
        if not isinstance(raw, dict) or set(raw) - {"id", "quantity", "staple"}:
            frappe.throw("餐品只能提交标识、数量与主食，金额由服务器计算。")
        item_id = raw.get("id")
        if not isinstance(item_id, str) or item_id not in by_id or item_id in seen:
            frappe.throw("餐品不存在或重复。")
        seen.add(item_id)
        item = by_id[item_id]
        quantity = _integer(raw.get("quantity"), "餐品数量", 1, 20)
        staple = _text(raw.get("staple"), "主食", 80)
        if item.get("staples"):
            if staple not in item["staples"]:
                frappe.throw("请选择菜单中的有效主食。")
        elif item.get("fixedStaple"):
            if staple and staple != item["fixedStaple"]:
                frappe.throw("固定主食不能替换。")
            staple = item["fixedStaple"]
        elif staple:
            frappe.throw("加料不能指定主食。")
        subtotal = item["priceCents"] * quantity
        total += subtotal
        main_count += quantity if item["category"] == "meal" else 0
        result.append({"id": item_id, "name": item["name"], "category": item["category"],
                       "priceCents": item["priceCents"], "quantity": quantity,
                       "staple": staple, "subtotalCents": subtotal})
    if not main_count:
        frappe.throw("加料不能单独下单，请至少选择一份主餐。")
    acknowledged = _boolean(budget_acknowledged)
    if total > BUDGET_CENTS and not acknowledged:
        frappe.throw("订单严格超过 50 元，请本人核对后确认超预算。")
    result.sort(key=lambda row: _ITEM_IDS.index(row["id"]))
    return {"items": result, "remarks": _text(remarks, "备注", 300, multiline=True),
            "total_cents": total, "over_budget": total > BUDGET_CENTS,
            "budget_acknowledged": acknowledged if total > BUDGET_CENTS else False}


@frappe.whitelist(methods=["POST"])
def submit_order(round, items, remarks="", expected_revision=0, budget_acknowledged=False, request_id=None):
    doc = _locked_round(round, participant=True)
    expected_revision = _integer(expected_revision, "订单版本", form=True)
    request_id = request_id or frappe.generate_hash(length=32)
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
        frappe.throw("请求标识须为 8 至 80 位字母、数字、下划线或短横线。")
    payload = canonical_order(_menu(doc), items, remarks, budget_acknowledged)
    payload_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    order = _own_order(doc)
    if order and order.last_request_id == request_id:
        if order.payload_sha256 != payload_hash:
            frappe.throw("同一请求标识不能用于不同订单。")
        return _order_dict(order)
    current = now_datetime()  # checked after all locks and current identity reads
    if not _is_open(doc, current):
        frappe.throw("工作餐尚未发布、已截止或已关闭，原订单已保留。")
    if order:
        if order.status != ACTIVE:
            frappe.throw("此订单已作废，不能由本人重新提交。请联系秘书。")
        _revision(order, expected_revision)
        order.revision = int(order.revision) + 1
    else:
        if expected_revision:
            frappe.throw("订单版本已变化，请刷新后核对。", frappe.TimestampMismatchError)
        order = frappe.get_doc({"doctype": "RG Meal Order", "round": doc.name, "member": frappe.session.user,
                                "submitted_at": current, "revision": 1, "status": ACTIVE})
    member = frappe.db.sql("SELECT display_name FROM `tabRG Member` WHERE user=%s AND active=1 FOR UPDATE",
                           (frappe.session.user,))
    if not member:
        deny()
    order.member_name, order.updated_at = member[0][0], current
    order.remarks, order.total_cents = payload["remarks"], payload["total_cents"]
    order.over_budget, order.budget_acknowledged_at = int(payload["over_budget"]), current if payload["over_budget"] else None
    order.payload_sha256, order.last_request_id, order.menu_sha256 = payload_hash, request_id, doc.menu_sha256
    order.set("items", [])
    for item in payload["items"]:
        order.append("items", {"item_id": item["id"], "item_name": item["name"], "category": item["category"],
                               "price_cents": item["priceCents"], "quantity": item["quantity"],
                               "staple": item["staple"], "subtotal_cents": item["subtotalCents"]})
    _save(order, new=order.is_new())
    return _order_dict(order)


@frappe.whitelist(methods=["POST"])
def void_order(order, reason, expected_revision):
    # Discover only the parent identity; do not return any order before authority.
    _guard((SECRETARY,))
    _text(order, "订单", 140, required=True)
    rows = frappe.db.sql("SELECT `round` FROM `tabRG Meal Order` WHERE name=%s LOCK IN SHARE MODE", (order,))
    if not rows:
        deny()
    _locked_round(rows[0][0], (SECRETARY,))
    doc = _load_order(order)
    _revision(doc, expected_revision)
    if doc.status != ACTIVE:
        frappe.throw("订单已经作废。")
    doc.void_reason = _text(reason, "作废原因", 300, required=True, multiline=True)
    doc.status, doc.voided_by, doc.voided_at = VOID, frappe.session.user, now_datetime()
    doc.updated_at, doc.revision = doc.voided_at, int(doc.revision) + 1
    _save(doc)
    return _order_dict(doc)


def _results(doc):
    rows = frappe.db.sql("SELECT name FROM `tabRG Meal Order` WHERE `round`=%s ORDER BY submitted_at,name "
                         f"LIMIT {MAX_ORDERS + 1} FOR UPDATE", (doc.name,))
    if len(rows) > MAX_ORDERS:
        frappe.throw("该轮次订单超过单次导出容量，请联系管理员。")
    all_orders = [_order_dict(_load_order(row[0])) for row in rows]
    active = [row for row in all_orders if row["status"] == ACTIVE]
    summaries = []
    for item in _menu(doc)["items"]:
        ordered = [entry for order in active for entry in order["items"] if entry["id"] == item["id"]]
        staples = {}
        for entry in ordered:
            if entry["staple"]:
                staples[entry["staple"]] = staples.get(entry["staple"], 0) + entry["quantity"]
        summaries.append({"id": item["id"], "name": item["name"], "category": item["category"],
                          "priceCents": item["priceCents"], "quantity": sum(row["quantity"] for row in ordered),
                          "totalCents": sum(row["subtotalCents"] for row in ordered), "staples": staples})
    return {"round": _round_dict(doc), "orders": active,
            "voided_orders": [row for row in all_orders if row["status"] == VOID],
            "order_count": len(active), "total_cents": sum(row["total_cents"] for row in active),
            "over_budget_count": sum(bool(row["over_budget"]) for row in active), "items": summaries}


@frappe.whitelist()
def round_results(round):
    return _results(_locked_round(round, (SECRETARY,)))


def csv_cell(value):
    value = str(value if value is not None else "").replace("\x00", "")
    candidate = value.lstrip(" \t\r\n\v\f\ufeff\u00a0\u200b")
    if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _csv_bytes(rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    for row in rows:
        writer.writerow([csv_cell(cell) for cell in row])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _export_rows(results):
    people = [["轮次", "标题", "状态", "账号", "姓名", "餐品", "主食", "份数", "单价（分）", "小计（分）",
               "订单合计（分）", "超50元", "备注", "首次提交", "最近修改", "修订", "作废人", "作废时间", "作废原因"]]
    dishes = [["轮次", "标题", "餐品", "主食", "份数", "单价（分）", "小计（分）"]]
    texts = []
    for result in results:
        rnd = result["round"]
        for order in result["orders"] + result["voided_orders"]:
            for item in order["items"]:
                people.append([rnd["name"], rnd["title"], order["status"], order["member"], order["member_name"],
                               item["name"], item["staple"], item["quantity"], item["priceCents"], item["subtotalCents"],
                               order["total_cents"], "是" if order["over_budget"] else "否", order["remarks"],
                               order["submitted_at"], order["updated_at"], order["revision"], order["voided_by"],
                               order["voided_at"], order["void_reason"]])
        lines = [rnd["title"], f"订单 {result['order_count']} 人；合计 {result['total_cents'] / 100:.2f} 元"]
        for item in result["items"]:
            if not item["quantity"]:
                continue
            counts = item["staples"] or {"": item["quantity"]}
            for staple, quantity in counts.items():
                dishes.append([rnd["name"], rnd["title"], item["name"], staple, quantity,
                               item["priceCents"], quantity * item["priceCents"]])
            detail = "；".join(f"{name} {quantity} 份" for name, quantity in item["staples"].items())
            lines.append(f"{item['name']} × {item['quantity']}" + (f"（{detail}）" if detail else ""))
        for order in result["orders"]:
            if order["remarks"]:
                lines.append(f"备注 {order['member_name']}：{order['remarks']}")
        texts.append("\n".join(lines))
    return people, dishes, "\n\n".join(texts)


def _download(filename, content, content_type):
    frappe.response.update(filename=filename, filecontent=content, type="download", content_type=content_type,
                           display_content_as="attachment")


@frappe.whitelist()
def merchant_text(round):
    return {"text": _export_rows([round_results(round)])[2]}


@frappe.whitelist()
def export_csv(round, kind="orders"):
    if kind not in ("orders", "items"):
        frappe.throw("导出类型须为 orders 或 items。")
    result = round_results(round)
    rows = _export_rows([result])[0 if kind == "orders" else 1]
    _download(f"{result['round']['name']}-{kind}.csv", _csv_bytes(rows), "text/csv; charset=utf-8")


@frappe.whitelist()
def export_zip(rounds):
    _guard((SECRETARY,))
    rounds = _json(rounds, "轮次清单")
    if (not isinstance(rounds, list) or not 1 <= len(rounds) <= MAX_ROUNDS
            or any(not isinstance(name, str) or not name or len(name) > 140 for name in rounds)
            or len(set(rounds)) != len(rounds)):
        frappe.throw("请选择 1 至 50 个不重复轮次。")
    results = [round_results(name) for name in sorted(rounds)]
    people, dishes, text = _export_rows(results)
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("逐人总表.csv", _csv_bytes(people))
        archive.writestr("按轮次菜品汇总.csv", _csv_bytes(dishes))
        archive.writestr("商家订单.txt", text.encode("utf-8"))
    _download("meal-rounds.zip", stream.getvalue(), "application/zip")


class _InternalMealDocument(Document):
    def validate(self):
        if self.flags.get("rg_meal") is not _TOKEN:
            deny()

    def on_trash(self):
        # Order mistakes are voided through the service, preserving evidence.
        deny()

    def notify_update(self):
        # Native list_update broadcasts names to a type room without row checks.
        # These records are exposed only by the authenticated meal service.
        pass


class RGMealRound(_InternalMealDocument):
    def check_if_latest(self):
        global_lock()
        return super().check_if_latest()

    def validate(self):
        super().validate()
        _require_current_roles((SECRETARY,))
        validate_menu(_menu_from_items(self))
        if self.is_new():
            if self.status != DRAFT or self.publisher != frappe.session.user or self.menu_snapshot or self.menu_sha256:
                deny()
            return
        old = self.get_doc_before_save()
        if self.publisher != old.publisher or self.published_at != old.published_at and old.status != DRAFT:
            deny()
        if old.status != DRAFT:
            if (self.title != old.title or self.meeting != old.meeting or self.menu_snapshot != old.menu_snapshot
                    or self.menu_sha256 != old.menu_sha256 or _menu_from_items(self) != _menu_from_items(old)):
                frappe.throw("发布后的菜单与内容不可修改。")


class RGMealItem(_InternalMealDocument):
    pass


class RGMealOrder(_InternalMealDocument):
    def check_if_latest(self):
        global_lock()
        if not self.is_new():
            rows = frappe.db.sql("SELECT `round` FROM `tabRG Meal Order` WHERE name=%s LOCK IN SHARE MODE", (self.name,))
            if rows:
                frappe.get_doc("RG Meal Round", rows[0][0], for_update=True)
        return super().check_if_latest()

    def validate(self):
        super().validate()
        if self.is_new():
            if self.member != frappe.session.user or self.status != ACTIVE:
                deny()
            _require_current_roles(ROLES, participant=True)
            return
        old = self.get_doc_before_save()
        if any(self.get(field) != old.get(field) for field in ("round", "member", "submitted_at", "menu_sha256")):
            deny()
        if self.status == VOID:
            _require_current_roles((SECRETARY,))
        elif self.member != frappe.session.user or old.status != ACTIVE:
            deny()


class RGMealOrderItem(_InternalMealDocument):
    pass

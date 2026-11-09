"""Real meal HTTP boundary, frozen cents, recovery, and exports; candidate only."""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
from io import BytesIO, StringIO
import json
import secrets
import sys
from zipfile import ZipFile

import role_http as h
import acceptance_config as cfg


def run(records):
    c = {key: h.Client().login(key) for key in h.ACCOUNTS}
    secretary, one, two, teacher = (c[k] for k in ("secretary", "one", "two", "teacher"))
    menu = h.success("default menu", h.rpc(one, "meals.default_menu"))
    h.check("7 mains and 7 addons", sum(i["category"] == "meal" for i in menu["items"]) == 7
            and sum(i["category"] == "addon" for i in menu["items"]) == 7)
    for item in menu["items"]:
        if item["category"] != "meal":
            continue
        status, _, raw = one.request(item["imageUrl"])
        expected = h.ROOT / "src/research_group/research_group/public/menu/2026-11-07" / (item["id"] + ".jpg")
        h.check("menu image original bytes " + item["id"], status == 200
                and hashlib.sha256(raw).digest() == hashlib.sha256(expected.read_bytes()).digest(), status=status)
    label = "Fictional meal HTTP " + secrets.token_hex(5)
    for client in (one, teacher):
        h.denied("only secretary creates rounds", h.rpc(client, "meals.create_round", title=label))
    create_path = "/api/method/research_group.meals.create_round"
    h.denied("correct CSRF external Origin", secretary.request(create_path, "POST", {"title": label},
             headers={"Origin": "https://outside.invalid"}))
    h.denied("missing Origin and Referer", secretary.request(create_path, "POST", {"title": label},
             headers={"Origin": None, "Referer": None}))
    h.denied("extra field cannot impersonate", h.rpc(secretary, "meals.create_round", title=label, actor=h.ACCOUNTS["teacher"]))
    h.denied("GET cannot create", secretary.request(create_path + "?title=no-write"))
    rnd = h.success("secretary creates draft", h.rpc(secretary, "meals.create_round", title=label))
    records["round"] = rnd["name"]
    for client in (one, teacher):
        h.denied("draft not visible to participant", h.rpc(client, "meals.get_round", name=rnd["name"]))
    rnd = h.success("publish frozen menu", h.rpc(secretary, "meals.publish_round", name=rnd["name"], expected_revision=rnd["revision"]))
    frozen_hash = rnd["menu_sha256"]
    h.check("published SHA", len(frozen_hash) == 64)
    h.denied("published menu cannot be edited", h.rpc(secretary, "meals.update_draft", name=rnd["name"],
             expected_revision=rnd["revision"], menu=menu))
    rice = [{"id": "meal-1", "quantity": 1, "staple": "白米饭"}]
    items49 = rice + [{"id": "addon-4", "quantity": 2}, {"id": "addon-1", "quantity": 2}]
    request_id = secrets.token_hex(12)
    first = h.success("49 yuan server cents", h.rpc(one, "meals.submit_order", round=rnd["name"], items=items49,
                      request_id=request_id))
    records["order"] = first["name"]
    h.check("49 below budget", first["total_cents"] == 4900 and not first["over_budget"])
    repeat = h.success("lost receipt retries same request", h.rpc(one, "meals.submit_order", round=rnd["name"],
                       items=items49, request_id=request_id))
    h.check("idempotency identity and revision", (repeat["name"], repeat["revision"]) == (first["name"], first["revision"]))
    h.denied("same request cannot change content", h.rpc(one, "meals.submit_order", round=rnd["name"],
             items=rice, request_id=request_id))
    h.denied("stale revision rejected", h.rpc(one, "meals.submit_order", round=rnd["name"], items=rice,
             expected_revision=0, request_id=secrets.token_hex(12)))
    order = h.success("50 yuan exact boundary", h.rpc(one, "meals.submit_order", round=rnd["name"],
                      items=[{**rice[0], "quantity": 2}], expected_revision=first["revision"], request_id=secrets.token_hex(12)))
    h.check("exactly 50 needs no acknowledgement", order["total_cents"] == 5000 and not order["over_budget"])
    items51 = rice + [{"id": "addon-4", "quantity": 2}, {"id": "addon-1", "quantity": 3}]
    h.denied("51 needs personal acknowledgement", h.rpc(one, "meals.submit_order", round=rnd["name"], items=items51,
             expected_revision=order["revision"], request_id=secrets.token_hex(12)))
    order = h.success("51 acknowledged", h.rpc(one, "meals.submit_order", round=rnd["name"], items=items51,
                      expected_revision=order["revision"], budget_acknowledged=True, remarks="=2+2",
                      request_id=secrets.token_hex(12)))
    h.check("same order history preserved", order["name"] == first["name"] and order["submitted_at"] == first["submitted_at"]
            and order["total_cents"] == 5100 and order["over_budget"] and bool(order["budget_acknowledged_at"]))
    invalid = [[{**rice[0], "quantity": q}] for q in (0, 21, 1.1, True, "1")]
    invalid += [[{"id": "addon-1", "quantity": 1}], [{**rice[0], "staple": "not on menu"}],
                [{**rice[0], "priceCents": 1}], rice + rice]
    for index, items in enumerate(invalid):
        h.denied("invalid meal rejected " + str(index), h.rpc(one, "meals.submit_order", round=rnd["name"],
                 items=items, expected_revision=order["revision"], request_id=secrets.token_hex(12)))
    recovered = h.success("recover saved order", h.rpc(one, "meals.get_round", name=rnd["name"]))
    h.check("invalid edits preserve receipt", recovered["my_order"]["revision"] == order["revision"]
            and recovered["my_order"]["total_cents"] == 5100 and recovered["menu_sha256"] == frozen_hash)
    h.check("other student sees no order", h.success("other own order", h.rpc(two, "meals.get_round", name=rnd["name"]))["my_order"] is None)
    other = h.success("second member fixed noodle staple", h.rpc(two, "meals.submit_order", round=rnd["name"],
                      items=[{"id": "meal-7", "quantity": 1}], request_id=secrets.token_hex(12)))
    h.check("fixed staple server selected", other["items"][0]["staple"] == "鸡蛋面")
    for client in (one, two, teacher):
        h.denied("participant cannot list others orders", h.rpc(client, "meals.round_results", round=rnd["name"]))
        h.denied("participant cannot export", h.rpc(client, "meals.export_csv", round=rnd["name"]))
    h.success("secretary voids with history", h.rpc(secretary, "meals.void_order", order=other["name"],
              reason="Fictional acceptance cancellation", expected_revision=other["revision"]))
    summary = h.success("secretary statistics", h.rpc(secretary, "meals.round_results", round=rnd["name"]))
    h.check("void excluded but retained", summary["order_count"] == 1 and summary["total_cents"] == 5100
            and summary["over_budget_count"] == 1 and len(summary["voided_orders"]) == 1)
    status, _, raw = h.rpc(secretary, "meals.export_csv", round=rnd["name"])
    rows = list(csv.reader(StringIO(raw.decode("utf-8-sig"))))
    h.check("CSV preserves quoted formula safely", status == 200 and any(row[12] == "'=2+2" for row in rows[1:]))
    merchant = h.success("merchant text", h.rpc(secretary, "meals.merchant_text", round=rnd["name"]))["text"]
    h.check("merchant text includes accurate sum", "51.00" in merchant)
    copy = h.success("copy frozen menu into new round", h.rpc(secretary, "meals.create_round", title=label + " copy", copy_from=rnd["name"]))
    records["open_round_for_ui"] = copy["name"]
    copy = h.success("publish UI round", h.rpc(secretary, "meals.publish_round", name=copy["name"], expected_revision=copy["revision"]))
    status, _, raw = h.rpc(secretary, "meals.export_zip", rounds=[rnd["name"], copy["name"]])
    with ZipFile(BytesIO(raw)) as archive:
        h.check("multi-round ZIP has all three summaries", status == 200 and len(archive.namelist()) == 3)
        h.check("multi-round retains original prices", b"2500" in archive.read("逐人总表.csv"))
    h.denied("ZIP rejects over 50 rounds", h.rpc(secretary, "meals.export_zip", rounds=["unused-" + str(i) for i in range(51)]))
    for client in (one, teacher, secretary):
        for doctype, name in (("RG Meal Round", rnd["name"]), ("RG Meal Order", order["name"])):
            h.denied("native internal read denied", client.get(doctype, name))
    rnd = h.success("manual close", h.rpc(secretary, "meals.set_round_status", name=rnd["name"], closed=True, expected_revision=rnd["revision"]))
    h.denied("closed round preserves existing order", h.rpc(one, "meals.submit_order", round=rnd["name"], items=rice,
             expected_revision=order["revision"], request_id=secrets.token_hex(12)))
    past = datetime.fromisoformat(rnd["server_time"]) - timedelta(seconds=1)
    rnd = h.success("deadline in past", h.rpc(secretary, "meals.set_deadline", name=rnd["name"],
                    deadline=past.isoformat(sep=" "), expected_revision=rnd["revision"]))
    h.denied("reopen cannot bypass cutoff", h.rpc(secretary, "meals.set_round_status", name=rnd["name"], closed=False, expected_revision=rnd["revision"]))
    final = h.success("read retained order after cutoff", h.rpc(one, "meals.get_round", name=rnd["name"]))
    h.check("receipt remains after cutoff", final["my_order"]["name"] == first["name"] and final["my_order"]["total_cents"] == 5100)


def main():
    records, error = {}, None
    try:
        run(records)
    except Exception as exc:
        error = type(exc).__name__
    output = h.save_receipt("meals-http-" + cfg.current().attempt + ".json", error, records)
    print(json.dumps({"ok": error is None, "checks": len(h.RESULTS), "receipt": str(output)}))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

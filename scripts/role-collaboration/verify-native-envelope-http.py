"""Preserve native large-survey saving while blocking internal document envelopes."""
import argparse
import json
import secrets
import sys
import urllib.parse

import role_http as h
import acceptance_config as cfg


def run(records):
    secretary = h.Client().login("secretary")
    label = "虚构HTTP原生大问卷-" + secrets.token_hex(5)
    options = "\n".join(f"{number:03d}" + "测" * 197 for number in range(100))
    document = {
        "doctype": "RG Survey", "title": label,
        "questions": [{"doctype": "RG Survey Question", "title": f"题目{number}",
                       "question_type": "单选", "required": 1, "options": options}
                      for number in range(10)],
    }
    encoded = json.dumps(document, ensure_ascii=False)
    h.check("合法问卷doc大于100KB并保留原题目与选项限制", len(encoded.encode("utf-8")) > 100000)
    created = h.success("原生insert接受合法大问卷doc字符串", secretary.request(
        "/api/method/frappe.client.insert", "POST", {"doc": encoded}))
    records["survey"] = created["name"]
    status, response, _ = secretary.get("RG Survey", created["name"])
    persisted = (response or {}).get("data", {})
    h.check("大问卷创建后原生重读完整10题各100个200字选项", status == 200
            and len(persisted.get("questions", [])) == 10
            and all(row["options"] == options for row in persisted["questions"]), status=status)
    persisted["description"] = "虚构问卷，验证旧版合法大doc保存不受新内部表保护误限。"
    saved = secretary.request("/api/method/frappe.desk.form.save.savedocs", "POST",
                              {"doc": json.dumps(persisted, ensure_ascii=False), "action": "Save"})
    h.check("原生Desk savedocs接受合法大问卷", saved[0] == 200, status=saved[0])
    status, response, _ = secretary.get("RG Survey", created["name"])
    persisted = (response or {}).get("data", {})
    h.check("大问卷保存后说明和完整选项重读不变", status == 200
            and persisted.get("description") == "虚构问卷，验证旧版合法大doc保存不受新内部表保护误限。"
            and all(row["options"] == options for row in persisted.get("questions", [])), status=status)
    h.success("大问卷保持旧发布校验并正常发布", h.rpc(secretary, "surveys.publish_survey", name=created["name"]))
    h.success("大问卷可按原流程关闭", h.rpc(secretary, "surveys.close_survey", name=created["name"]))
    for key in ("doc", "docs", "data"):
        for form, value in (("dict", {"doctype": "RG Message", "body": "x" * 100001}),
                            ("list", [{"doctype": "RG Survey"}] * 101 + [{"doctype": "RG Message"}])):
            h.denied("内部表大/末位原生envelope拒绝 " + key + " " + form,
                     secretary.request("/api/method/frappe.client.insert_many", "POST",
                                       {key: json.dumps(value)}), statuses=(403,))


def main():
    records, error = {}, None
    try:
        run(records)
    except Exception as exc:
        error = type(exc).__name__
    output = h.save_receipt("native-envelope-http-" + cfg.current().attempt + ".json", error, records)
    print(json.dumps({"ok": error is None, "checks": len(h.RESULTS), "receipt": str(output)}))
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

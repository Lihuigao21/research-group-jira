"""LangChain model transport and deterministic offline plans; no persistent SDK state."""
import asyncio
import copy
import importlib.metadata
import logging
import os
import time
from urllib.parse import urlsplit

import frappe

from research_group.ai.config import limits, mode, server_configuration
from research_group.ai.context import INSTRUCTIONS
from research_group.ai.storage import dumps
from research_group.ai.tools import DESCRIPTIONS, SCHEMAS, available
from research_group.policy import SECRETARY, STUDENT, TEACHER

VERSIONS = {"langchain": "1.4.9", "langchain-core": "1.7.6", "langgraph": "1.2.20",
            "langchain-openai": "1.7.4", "openai": "2.45.0"}


class OfflineProvider:
    """确定性演示路由，不模拟真实模型的理解、质量或计费。"""

    def plan(self, ctx, prompt):
        assigned = ctx.check()['roles']
        if ctx.selected_selections:
            selection_id = next(iter(ctx.selected_selections))
            selection = (yield ('read_manuscript_selection', {'selection': selection_id}))
            yield ('propose_manuscript_suggestion', {
                'selection': selection_id, 'suggestion': selection['original_text'],
                'reason': '离线模拟保留原文，仅演示选段授权、建议预览和本人确认；未调用真实模型，不能据此判断润色质量。',
            })
            answer = '已生成本人选区的AI辅助建议预览，原稿保留。'
        elif any(word in prompt for word in ('如何', '怎么', '哪里', '在哪', '怎样')):
            features = (('materials', ('PPT', '材料', '上传')), ('appointments', ('预约', '讨论')),
                        ('surveys', ('问卷', '投票')), ('meals', ('工作餐', '订餐')),
                        ('accounts', ('账号', '账户', '邮箱', '密码')),
                        ('messages', ('邮件', '通知', '群发')), ('manuscripts', ('论文', '选区', '选段', '稿件')))
            feature = next((name for name, terms in features if any(term in prompt for term in terms)), 'portals')
            guide = (yield ('guide_site', {'feature': feature}))
            answer = guide['title'] + '：' + guide['description'] + '\n入口：' + guide['url']
        elif prompt.strip().startswith(('记住', '请记住')):
            content = prompt.strip().removeprefix('请').removeprefix('记住').lstrip('：:，, ')
            yield ('propose_memory_change', {'change': 'add', 'kind': 'preference', 'content': content, 'topic': ctx.topic or ''})
            answer = '已生成记忆候选，请核对操作卡并确认后保存。未经确认不会进入长期记忆；候选7天过期，操作卡15分钟过期。'
        elif TEACHER in assigned and any((word in prompt for word in ('全组', '最近讨论', '每个人', '总览'))):
            result = (yield ('get_group_discussion_overview', {'limit': 5}))
            coverage = f"本页覆盖 {result['covered_students']}/{result['total_students']} 位学生；"
            if result['unexpanded_students']:
                coverage += f"尚未展开 {result['unexpanded_students']} 位，不能把本页称为全组结论。"
            elif result['total_students']:
                coverage += '本次查询已覆盖全部学生；此总览仅代表平台正式记录，未记录不表示没有发生。'
            else:
                coverage += '平台暂无本次查询范围内的学生记录，不能据此认定没有讨论。'
            lines = [f"正式讨论范围：{result['from_date']} 至 {result['to_date']}（Asia/Shanghai）。", coverage]
            for student in result['students']:
                lines.append('\n' + student['display_name'] + '：')
                if not student['topics']:
                    lines.append('平台暂无正式课题记录。')
                for entry in student['topics']:
                    (topic, discussion, progress) = (entry['topic'], entry['discussion_in_range'], entry['latest_progress'])
                    lines.append('课题：' + topic['title'] + '（' + topic['name'] + '）')
                    if discussion:
                        lines.append('最近讨论：' + str(discussion['discussed_at']))
                        lines.append('教师反馈：' + (discussion.get('teacher_feedback') or '尚无教师受控反馈字段内容'))
                        lines.append(discussion['field_sources']['actions']['kind'] + '的后续行动：' + str(discussion['actions']))
                    else:
                        lines.append(entry['gap'])
                    if entry['latest_discussion_outside_range']:
                        lines.append('范围外最近讨论：' + entry['latest_discussion_outside_range'])
                    lines.append('最新已记录进展：' + (progress['summary'] if progress else '平台暂无正式进展记录'))
                    if progress and progress.get('blockers'):
                        lines.append('已记录阻碍：' + progress['blockers'])
                if student['more_topics']:
                    lines.append('该学生还有未展开课题，请继续查询。')
            answer = '\n'.join(lines)
        elif any((word in prompt for word in ('组会', '排期', '日程'))):
            result = (yield ('get_meeting_schedule', {}))
            lines = ['未来30天获准排期（Asia/Shanghai）：']
            for meeting in result['meetings']:
                lines.append(f"{meeting['title']} · {meeting['start_at']} · {meeting['location']} · 材料截止 {meeting['deadline']}")
                report = meeting['my_report']
                if report:
                    lines.append('你的报告任务：' + (report['topic'] or '尚未提交主题') + '；最近材料版本 ' + (report['latest_version'] or '0'))
                if meeting['more_reports']:
                    lines.append('报告列表仅展开前20项，其余未展开；本人报告任务已单独查询。')
            if not result['meetings']:
                lines.append('平台暂无该范围内获准组会记录。')
            if result['has_more']:
                lines.append('本页仍有未展开排期，请缩小日期范围或分页。')
            lines.append(result['notice'])
            answer = '\n'.join(lines)
        elif '问卷' in prompt or '投票' in prompt:
            found = (yield ('get_survey_for_me', {'limit': 5}))
            lines = ['本人可见问卷：']
            for survey in found['surveys']:
                lines.append(survey['title'] + '（' + survey['status'] + '，' + survey['name'] + '）')
            if SECRETARY in assigned and any((word in prompt for word in ('概况', '统计', '汇总', '完成'))):
                for survey in found['surveys'][:4]:
                    result = (yield ('get_survey_summary', {'name': survey['name']}))
                    lines.append(f"{result['title']}：累计收取{result['submitted_count']}份；当前成员{result['active_submitted_count']}/{result['active_member_count']}已填，待填{result['pending_count']}人。")
                    for question in result['questions']:
                        lines.append(question['title'] + '：' + str(question['answered_count']) + '人作答')
                        for option in question.get('options', []):
                            lines.append('  ' + option['label'] + ' ' + str(option['count']))
                lines.append('只汇总本页前4份；不含实名简答、他人原答卷或CSV。')
            if not found['surveys']:
                lines.append('平台暂无获准问卷。')
            if found['has_more']:
                lines.append('还有未展开问卷，请在原问卷页面选择具体记录。')
            answer = '\n'.join(lines)
        elif STUDENT in assigned and any((word in prompt for word in ('反馈', '安排工作', '计划', '下一步'))):
            answer = (yield from self.student_plan(ctx, prompt))
        elif '记忆' in prompt:
            result = (yield ('search_my_memory', {}))
            answer = '本人有效记忆：\n' + '\n'.join((row['content'] for row in result.get('memories', [])))
            if not result.get('memories'):
                answer += '暂无可用条目，或跨会话记忆已关闭。'
        else:
            answer = '站内任务：' + prompt[:500] + '\n\n当前是离线模拟，可演示聊天保存、来源查询、记忆、计划和确认操作。本助手仅处理站内工作；离线模拟不代表真实模型质量。可使用上方快捷入口，或输入“记住我希望先看结论”。'
        ctx.usage = 0
        return '【离线模拟】\n' + answer

    def student_plan(self, ctx, prompt):
        if ctx.topic:
            topic = ctx.topic
        else:
            topics = (yield ('search_research_records', {'record_type': 'topic', 'limit': 3}))
            rows = topics['records']
            if len(rows) != 1 or topics['has_more']:
                return '请先在课题选择框指定一个课题，避免混合不同反馈。\n' + '\n'.join((row['name'] + '：' + row['title'] for row in rows))
            topic = rows[0]['name']
        discussions = (yield ('search_research_records', {'record_type': 'discussion', 'topic': topic, 'limit': 1}))
        progress = (yield ('search_research_records', {'record_type': 'progress', 'topic': topic, 'limit': 1}))
        plans = (yield ('get_my_plan', {'limit': 5}))
        lines = ['课题：' + topic]
        if discussions['records']:
            doc = discussions['records'][0]
            lines.append('正式讨论：' + doc['name'] + ' · ' + str(doc['discussed_at']))
            lines.append('老师明确要求（教师受控反馈字段）：' + (doc.get('teacher_feedback') or '平台暂无可核验教师反馈，需再次确认'))
            lines.append(doc['field_sources']['actions']['kind'] + '的结论/行动：' + doc['conclusion'] + '；' + doc['actions'])
        else:
            lines.append('平台暂无正式讨论记录，不能据此认定没有讨论；需要先确认最近反馈。')
        if progress['records']:
            doc = progress['records'][0]
            lines.append('你已记录的进展：' + doc['summary'] + '（' + doc['name'] + '，' + str(doc['progress_date']) + '）')
            if doc.get('blockers'):
                lines.append('已记录阻碍：' + doc['blockers'])
        lines.extend(['AI建议（模拟示例，以下方法和期限需本人核对）：', '1. 先逐条核对最近教师反馈与已完成记录，列出尚未确认的问题。', '2. 对每个待办明确一个可执行步骤、依赖和预期交付物；涉及对照时先确认控制变量，不凭空指定参数。', '3. 用记录、图表或可复现输入核验交付物，再整理差异与待讨论问题。', '4. 本周内的优先级和期限是建议；冲突反馈、资源和验收标准应向老师再次确认。', '个人完成标记不代表科研成功、正式进展或教师认可。'])
        if plans.get('plans'):
            lines.append('本人已有有效计划：' + '、'.join((row['name'] for row in plans['plans'] if row.get('topic') in {'', topic, None})))
        answer = '\n'.join(lines)
        if '保存' in prompt:
            yield ('propose_plan_change', {'change': 'add', 'topic': topic, 'content': answer, 'status': 'active'})
            answer += '\n已生成私人计划预览，请核对含义后在操作卡确认。'
        return answer

    def session(self, ctx, prompt):
        return _OfflineSession(ctx, self.plan(ctx, prompt))

    def run(self, ctx, prompt):
        from research_group.ai.graph import run_graph
        return run_graph(ctx, prompt, self)


class _OfflineSession:
    structured = False

    def __init__(self, ctx, plan):
        self.ctx, self.plan, self.started = ctx, plan, False

    def step(self, results):
        self.ctx.check()
        if not self.started:
            self.ctx.model_start()
            self.started = True
        try:
            value = self.plan.send(results[0]["data"]) if results else next(self.plan)
        except StopIteration as final:
            self.ctx.usage = 0
            return {"finished": True, "answer": final.value}
        name, arguments = value
        return {"calls": [{"name": name, "args": arguments, "id": "offline-" + str(len(self.ctx.tool_order) + 1)}]}

    def close(self):
        self.plan.close()
        self.ctx.usage = 0


def strict_tools(names):
    """Copy schemas before adding strict-required nullable optional fields."""
    result = []
    for name in names:
        definition = copy.deepcopy(SCHEMAS[name])
        required = set(definition["required"])
        for key, value in definition["properties"].items():
            if key not in required:
                types = value["type"] if isinstance(value["type"], list) else [value["type"]]
                value["type"] = list(dict.fromkeys([*types, "null"]))
                if "enum" in value and None not in value["enum"]:
                    value["enum"].append(None)
        definition["required"] = list(definition["properties"])
        result.append({"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
                                                         "parameters": definition, "strict": True}})
    return result


def disable_tracing():
    # No LangSmith environment configuration may enable background content export.
    for name in ("LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        os.environ[name] = "false"
    for name in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
        os.environ.pop(name, None)
    for name in ("langchain", "langsmith", "openai", "httpx", "httpcore"):
        logging.getLogger(name).disabled = True


class LangChainProvider:
    """ChatOpenAI is the only real-model adapter; no built-in or discovered tools."""

    def __init__(self, transport=None):
        # Python tests can replace the complete HTTP transport. No HTTP parameter
        # exposes endpoint/model/transport to a user or a model-generated call.
        self.transport = transport

    def session(self, ctx, prompt):
        return _LangChainSession(ctx, self.transport)

    def run(self, ctx, prompt):
        from research_group.ai.graph import run_graph
        return run_graph(ctx, prompt, self)


class _LangChainSession:
    structured = True

    def __init__(self, ctx, transport):
        disable_tracing()
        self.ctx, self.transport = ctx, transport
        self.messages = None
        self.last_response = None

    def step(self, results):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        from langchain_openai import ChatOpenAI
        import httpx

        self.ctx.check()
        configuration = server_configuration()
        if not configuration.get("rg_ai_real_enabled"):
            frappe.throw("真实模型调用尚未启用；请使用离线模拟。")
        if any(importlib.metadata.version(name) != version for name, version in VERSIONS.items()):
            frappe.throw("LangChain/LangGraph/OpenAI版本与固定适配器不符。")
        endpoint = configuration.get("rg_ai_base_url") or ""
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or endpoint not in (configuration.get("rg_ai_allowed_base_urls") or [])):
            frappe.throw("模型端点未在服务端批准清单内。")
        model, key = configuration.get("rg_ai_model"), configuration.get("rg_ai_api_key")
        protocol = configuration.get("rg_ai_protocol", "responses")
        if not model or not key or protocol not in {"responses", "chat_completions"}:
            frappe.throw("共享模型服务尚未配置完整。")
        if self.messages is None:
            convert = {"user": HumanMessage, "assistant": AIMessage}
            self.messages = [SystemMessage(content=INSTRUCTIONS)] + [
                convert[row["role"]](content=row["content"]) for row in self.ctx.inputs if row["role"] in convert]
            self.ctx.usage = 0
        elif results:
            self.messages.extend(ToolMessage(content=dumps(item["data"]), tool_call_id=item["id"], name=item["name"])
                                 for item in results)
        self.ctx.model_start()

        async def invoke():
            remaining = max(0.1, limits()["run_seconds"] - (time.monotonic() - self.ctx.started))

            async def check_request(request):
                if len(request.content) > limits()["context_tokens"]:
                    frappe.throw("模型请求（含工具定义）达到本轮上下文上限。")
                self.ctx.check()
                frappe.db.commit()

            async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False,
                                         transport=self.transport, event_hooks={"request": [check_request]}) as client:
                # Explicit clients stop environment proxies, retries and redirect
                # behavior from expanding the approved outbound surface.
                with httpx.Client(timeout=remaining, follow_redirects=False, trust_env=False) as sync_client:
                    adapter = ChatOpenAI(model=model, api_key=key, base_url=endpoint, timeout=remaining,
                                         max_retries=0, http_async_client=client, http_client=sync_client,
                                         use_responses_api=protocol == "responses", output_version="v0",
                                         use_previous_response_id=False, store=False,
                                         max_tokens=limits()["output_tokens"], callbacks=[],
                                         include_response_headers=False)
                    bound = adapter.bind_tools(strict_tools(available(self.ctx.check()["roles"])),
                                               strict=True, parallel_tool_calls=False)
                    return await asyncio.wait_for(bound.ainvoke(self.messages, config={"callbacks": []}), remaining)

        try:
            # tracing_context(False) also overrides an already initialized
            # LangSmith global context; environment flags alone are insufficient.
            from langsmith import tracing_context
            with tracing_context(enabled=False):
                response = asyncio.run(invoke())
        except (asyncio.TimeoutError, httpx.TimeoutException) as error:
            raise TimeoutError("bounded model deadline") from error
        self.ctx.check()
        usage = response.usage_metadata or {}
        total = usage.get("total_tokens")
        if type(total) is int and total > 0 and self.ctx.usage is not None:
            self.ctx.usage += total
        else:
            self.ctx.usage = None
        frappe.db.commit()
        if response.invalid_tool_calls:
            frappe.throw("模型工具参数无效。")
        self.messages.append(response)
        calls = [{"name": call["name"], "args": call["args"], "id": call["id"]} for call in response.tool_calls]
        if calls:
            return {"calls": calls}
        content = response.content
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict)
                              and item.get("type") in {"text", "output_text"})
        return {"answer": content, "finished": True}


# Keep Python import compatibility for existing clients/tests. This name has no
# Agents SDK dependency and cannot select an alternate orchestration path.
AgentsProvider = LangChainProvider


def get_provider():
    value = mode()
    if value == "disabled":
        frappe.throw("AI助手当前已关闭。")
    return OfflineProvider() if value == "mock" else LangChainProvider()

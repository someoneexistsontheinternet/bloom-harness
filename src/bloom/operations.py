from __future__ import annotations

from .call_views import export_calls


def report(store):
    return {
        "tasks": store.rows(
            "SELECT id,agent_id,parent_id,status,objective,turns,error FROM tasks ORDER BY created_at DESC LIMIT 30"
        ),
        "owner_requests": store.rows(
            "SELECT id,operation,status,task_id,result_hash,source_path,accepted_at FROM owner_requests ORDER BY rowid DESC LIMIT 30"
        ),
        "processes": store.rows(
            "SELECT id,agent_id,target,status,pid,exit_code FROM processes WHERE status!='completed'"
        ),
        "unresolved_effects": store.rows(
            "SELECT id,task_id,name,status,error FROM operations WHERE status='uncertain'"
        ),
        "unresolved_deliveries": store.rows(
            "SELECT id,task_id,status,error FROM deliveries WHERE status IN ('uncertain','failed')"
        ),
        "pending_deliveries": store.rows(
            "SELECT task_id,count(*) AS messages FROM deliveries WHERE status='prepared' GROUP BY task_id"
        ),
        "unresolved_routes": store.rows(
            "SELECT task_id,status,error FROM channel_routes WHERE status!='ready'"
        ),
        "usage": store.rows(
            "SELECT agent_id,purpose,count(*) AS attempts,sum(input_tokens) AS input_tokens,sum(cached_tokens) AS cached_tokens,sum(output_tokens) AS output_tokens,sum(cost_usd) AS known_cost_usd,sum(cost_usd IS NULL) AS unpriced_calls,sum(input_tokens IS NULL OR output_tokens IS NULL) AS unknown_usage_calls FROM model_calls GROUP BY agent_id,purpose"
        ),
    }

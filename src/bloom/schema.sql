PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(
 id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('astra','sol','luna')), parent_id TEXT REFERENCES agents(id),
 name TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('full','workspace','read')), network INTEGER NOT NULL DEFAULT 0,
 cwd TEXT NOT NULL, created_at TEXT NOT NULL, CHECK(role!='astra' OR id='astra'));
CREATE UNIQUE INDEX IF NOT EXISTS one_astra ON agents(role) WHERE role='astra';
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE REFERENCES agents(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_environment(agent_id TEXT REFERENCES agents(id), name TEXT NOT NULL, PRIMARY KEY(agent_id,name));
CREATE TABLE IF NOT EXISTS browser_sessions(agent_id TEXT PRIMARY KEY REFERENCES agents(id), driver_session TEXT NOT NULL, conversation_url TEXT NOT NULL, input_count INTEGER NOT NULL, prefix_hash TEXT NOT NULL, pending_call TEXT, model_label TEXT, effort_label TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), parent_id TEXT REFERENCES tasks(id), objective TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ready','running','waiting','paused','blocked','completed','cancelled')),
 plan TEXT NOT NULL DEFAULT '', turns INTEGER NOT NULL DEFAULT 0, max_turns INTEGER NOT NULL DEFAULT 500,
 channel TEXT NOT NULL DEFAULT 'local', address TEXT NOT NULL DEFAULT '', error TEXT, final_item INTEGER,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(hash TEXT PRIMARY KEY, bytes INTEGER NOT NULL, media_type TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_layout(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version IN (1,2)));
CREATE TABLE IF NOT EXISTS artifact_access(hash TEXT PRIMARY KEY REFERENCES artifacts(hash), accessed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS archive_files(id INTEGER PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, mode INTEGER NOT NULL, kind TEXT NOT NULL, copied_at TEXT NOT NULL, indexed INTEGER NOT NULL DEFAULT 0, removed_at TEXT, UNIQUE(path,sha256));
CREATE TABLE IF NOT EXISTS archive_chunks(id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES archive_files(id), position INTEGER NOT NULL, text_zlib BLOB NOT NULL, UNIQUE(file_id,position));
CREATE VIRTUAL TABLE IF NOT EXISTS archive_search USING fts5(path,text,content='');
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), agent_id TEXT NOT NULL REFERENCES agents(id), purpose TEXT NOT NULL,
 model_requested TEXT NOT NULL, effort TEXT, provider TEXT NOT NULL, model_reported TEXT, response_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('prepared','running','completed','failed','uncertain')),
 request_hash TEXT REFERENCES artifacts(hash), response_hash TEXT REFERENCES artifacts(hash),
 input_tokens INTEGER, cached_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, cost_usd REAL,
 started_at TEXT NOT NULL, completed_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS items(
 id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), task_id TEXT REFERENCES tasks(id),
 model_call_id TEXT REFERENCES model_calls(id), source_key TEXT UNIQUE,
 kind TEXT NOT NULL, role TEXT, phase TEXT, provider_id TEXT, call_id TEXT, tool_name TEXT, text TEXT NOT NULL DEFAULT '',
 envelope_hash TEXT NOT NULL REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_call_streams(call_id TEXT PRIMARY KEY REFERENCES model_calls(id), stream_hash TEXT NOT NULL REFERENCES artifacts(hash));
CREATE TABLE IF NOT EXISTS model_call_diagnostics(call_id TEXT PRIMARY KEY REFERENCES model_calls(id), detail_hash TEXT NOT NULL REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_text_streams(call_id TEXT PRIMARY KEY REFERENCES model_calls(id), task_id TEXT NOT NULL REFERENCES tasks(id), text TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_previews(task_id TEXT PRIMARY KEY REFERENCES tasks(id), delivery_id TEXT NOT NULL REFERENCES deliveries(id), call_id TEXT NOT NULL REFERENCES model_calls(id), revision INTEGER NOT NULL, retry_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS model_reconciliations(call_id TEXT PRIMARY KEY REFERENCES model_calls(id), original_hash TEXT NOT NULL REFERENCES artifacts(hash), decision TEXT NOT NULL, reason TEXT NOT NULL, authorized_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_backups(id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, sha256 TEXT, bytes INTEGER, artifact_count INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS items_session_order ON items(session_id,id);
CREATE INDEX IF NOT EXISTS items_task ON items(task_id,id);
CREATE TABLE IF NOT EXISTS model_call_items(call_id TEXT REFERENCES model_calls(id), item_id INTEGER REFERENCES items(id), position INTEGER NOT NULL, PRIMARY KEY(call_id,position));
CREATE TABLE IF NOT EXISTS model_call_sources(call_id TEXT REFERENCES model_calls(id), item_id INTEGER REFERENCES items(id), position INTEGER NOT NULL, PRIMARY KEY(call_id,position));
CREATE TABLE IF NOT EXISTS owner_turns(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), source_item INTEGER NOT NULL UNIQUE REFERENCES items(id), created_at TEXT NOT NULL, reply_item INTEGER REFERENCES items(id), replied_at TEXT);
CREATE TABLE IF NOT EXISTS task_owner_turns(task_id TEXT PRIMARY KEY REFERENCES tasks(id), turn_id TEXT NOT NULL REFERENCES owner_turns(id));
CREATE TABLE IF NOT EXISTS model_call_turns(call_id TEXT PRIMARY KEY REFERENCES model_calls(id), turn_id TEXT NOT NULL REFERENCES owner_turns(id));
CREATE TABLE IF NOT EXISTS operation_turns(operation_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES owner_turns(id));
CREATE TABLE IF NOT EXISTS tool_result_files(operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), agent_id TEXT NOT NULL REFERENCES agents(id), turn_id TEXT REFERENCES owner_turns(id), tool_id TEXT NOT NULL, tool_name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL REFERENCES artifacts(hash), text_hash TEXT NOT NULL, characters INTEGER NOT NULL, metadata_hash TEXT NOT NULL REFERENCES artifacts(hash), status TEXT NOT NULL, summary_hash TEXT REFERENCES artifacts(hash), curator_call TEXT REFERENCES model_calls(id), created_at TEXT NOT NULL, accessed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS research_views(operation_id TEXT PRIMARY KEY REFERENCES operations(id), task_id TEXT NOT NULL REFERENCES tasks(id), agent_id TEXT NOT NULL REFERENCES agents(id), source_hash TEXT NOT NULL REFERENCES artifacts(hash), view_hash TEXT NOT NULL REFERENCES artifacts(hash), image_policy_hash TEXT NOT NULL REFERENCES artifacts(hash), text_tokens INTEGER NOT NULL, image_count INTEGER NOT NULL, budget INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls(
 id TEXT PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES items(id), task_id TEXT NOT NULL REFERENCES tasks(id),
 name TEXT NOT NULL, arguments_hash TEXT NOT NULL REFERENCES artifacts(hash), operation_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS tool_results(
 call_id TEXT PRIMARY KEY REFERENCES tool_calls(id), item_id INTEGER NOT NULL UNIQUE REFERENCES items(id), is_error INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL, name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','running','completed','failed','uncertain','cancelled')),
 intent_hash TEXT NOT NULL REFERENCES artifacts(hash), result_hash TEXT REFERENCES artifacts(hash),
 started_at TEXT NOT NULL, completed_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS file_operations(operation_id TEXT PRIMARY KEY REFERENCES operations(id), task_id TEXT REFERENCES tasks(id), agent_id TEXT REFERENCES agents(id), path TEXT NOT NULL, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_batches(operation_id TEXT PRIMARY KEY REFERENCES operations(id), parent_task TEXT NOT NULL REFERENCES tasks(id), request_hash TEXT NOT NULL REFERENCES artifacts(hash), steering_epoch INTEGER NOT NULL, deadline REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_workers(task_id TEXT PRIMARY KEY REFERENCES tasks(id), operation_id TEXT NOT NULL REFERENCES evidence_batches(operation_id), position INTEGER NOT NULL, UNIQUE(operation_id,position));
CREATE TABLE IF NOT EXISTS task_outcomes(task_id TEXT PRIMARY KEY REFERENCES tasks(id), status TEXT NOT NULL, reason TEXT NOT NULL, evidence_hash TEXT REFERENCES artifacts(hash));
CREATE TABLE IF NOT EXISTS task_notifications(task_id TEXT PRIMARY KEY REFERENCES tasks(id), fingerprint TEXT NOT NULL, item_id INTEGER REFERENCES items(id));
CREATE TABLE IF NOT EXISTS task_assignments(operation_id TEXT PRIMARY KEY REFERENCES operations(id), parent_task TEXT NOT NULL REFERENCES tasks(id), child_task TEXT NOT NULL UNIQUE REFERENCES tasks(id), request_hash TEXT NOT NULL REFERENCES artifacts(hash), background INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_grants(agent_id TEXT NOT NULL REFERENCES agents(id), artifact_hash TEXT NOT NULL REFERENCES artifacts(hash), path TEXT NOT NULL, content_sha256 TEXT, granted_by TEXT NOT NULL REFERENCES agents(id), source_task TEXT NOT NULL REFERENCES tasks(id), created_at TEXT NOT NULL, PRIMARY KEY(agent_id,artifact_hash,path));
CREATE TABLE IF NOT EXISTS semantic_upkeep(batch_id TEXT PRIMARY KEY REFERENCES memory_batches(id), call_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_upkeep_due(agent_id TEXT PRIMARY KEY REFERENCES agents(id), watermark INTEGER NOT NULL, eligible_tokens INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_pauses(task_id TEXT PRIMARY KEY REFERENCES tasks(id), pause_root TEXT NOT NULL REFERENCES tasks(id), previous_status TEXT NOT NULL, paused_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS item_origins(item_id INTEGER PRIMARY KEY REFERENCES items(id), kind TEXT NOT NULL, source_task TEXT REFERENCES tasks(id), source_agent TEXT REFERENCES agents(id));
CREATE TABLE IF NOT EXISTS processes(
 id TEXT PRIMARY KEY, operation_id TEXT UNIQUE REFERENCES operations(id), agent_id TEXT REFERENCES agents(id), target TEXT NOT NULL,
 cwd TEXT NOT NULL, command_hash TEXT NOT NULL REFERENCES artifacts(hash), pid INTEGER, start_identity TEXT,
 status TEXT NOT NULL, exit_code INTEGER, directory TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS process_awaits(
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 process_id TEXT REFERENCES processes(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id), agent_id TEXT REFERENCES agents(id), kind TEXT NOT NULL,
 detail_hash TEXT REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS process_targets(process_id TEXT PRIMARY KEY REFERENCES processes(id), kind TEXT NOT NULL, host TEXT, port INTEGER, root TEXT, python TEXT, identity_file TEXT, known_hosts TEXT);
CREATE TABLE IF NOT EXISTS contexts(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), parent_id TEXT REFERENCES contexts(id),
 summary_item INTEGER REFERENCES items(id), through_item INTEGER NOT NULL, token_estimate INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS context_members(
 context_id TEXT REFERENCES contexts(id), item_id INTEGER REFERENCES items(id), position INTEGER NOT NULL,
 PRIMARY KEY(context_id,item_id), UNIQUE(context_id,position));
CREATE TABLE IF NOT EXISTS memories(topic TEXT PRIMARY KEY, revision INTEGER NOT NULL, summary TEXT NOT NULL, detail_hash TEXT REFERENCES artifacts(hash), updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_revisions(topic TEXT NOT NULL, revision INTEGER NOT NULL, summary TEXT NOT NULL, detail_hash TEXT REFERENCES artifacts(hash), rationale TEXT NOT NULL, source_hash TEXT REFERENCES artifacts(hash), created_at TEXT NOT NULL, PRIMARY KEY(topic,revision));
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, item_id INTEGER REFERENCES items(id), task_id TEXT REFERENCES tasks(id), channel TEXT NOT NULL,
 address TEXT NOT NULL, part INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message',
 status TEXT NOT NULL CHECK(status IN ('prepared','sending','sent','failed','uncertain')),
 external_id TEXT, nonce TEXT NOT NULL UNIQUE, error TEXT, created_at TEXT NOT NULL, sent_at TEXT,
 UNIQUE(item_id,channel,address,part));
CREATE TABLE IF NOT EXISTS waits(task_id TEXT PRIMARY KEY REFERENCES tasks(id), deadline REAL NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_payloads(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id), payload_hash TEXT NOT NULL REFERENCES artifacts(hash));
-- Presence (even an empty list) selects snapshot ordering; absent rows retain legacy order.
CREATE TABLE IF NOT EXISTS delivery_ordering(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id), predecessors_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity_feed(singleton INTEGER PRIMARY KEY CHECK(singleton=1), channel_id TEXT NOT NULL, after_call_rowid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS activity_maintenance_feed(singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_call_rowid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS activity_records(call_id TEXT NOT NULL REFERENCES model_calls(id), stage TEXT NOT NULL, PRIMARY KEY(call_id,stage));
CREATE TABLE IF NOT EXISTS wait_children(task_id TEXT REFERENCES waits(task_id) ON DELETE CASCADE, child_id TEXT REFERENCES tasks(id), PRIMARY KEY(task_id,child_id));
CREATE TABLE IF NOT EXISTS wait_processes(task_id TEXT REFERENCES waits(task_id) ON DELETE CASCADE, process_id TEXT REFERENCES processes(id), PRIMARY KEY(task_id,process_id));
CREATE TABLE IF NOT EXISTS wait_conditions(task_id TEXT PRIMARY KEY REFERENCES waits(task_id) ON DELETE CASCADE, after_event INTEGER NOT NULL, owner_messages INTEGER NOT NULL, not_before REAL NOT NULL);
CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY, command TEXT NOT NULL, request_hash TEXT REFERENCES artifacts(hash), status TEXT NOT NULL, result_hash TEXT REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation_controls(operation_id TEXT PRIMARY KEY REFERENCES operations(id), control_id TEXT NOT NULL REFERENCES controls(id));
CREATE TABLE IF NOT EXISTS owner_requests(id TEXT PRIMARY KEY, source_hash TEXT NOT NULL REFERENCES artifacts(hash), operation TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed','failed')), task_id TEXT REFERENCES tasks(id), result_hash TEXT NOT NULL REFERENCES artifacts(hash), source_path TEXT NOT NULL, accepted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worktrees(id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), repository TEXT NOT NULL, path TEXT NOT NULL UNIQUE, branch TEXT NOT NULL, base_commit TEXT NOT NULL, submitted_commit TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY, repository TEXT NOT NULL, before_commit TEXT NOT NULL, candidate_commit TEXT NOT NULL, status TEXT NOT NULL, evidence_hash TEXT REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_worktrees(release_id TEXT PRIMARY KEY REFERENCES releases(id), worktree_id TEXT NOT NULL REFERENCES worktrees(id));
CREATE TABLE IF NOT EXISTS release_reviews(release_id TEXT PRIMARY KEY REFERENCES releases(id), agent_id TEXT REFERENCES agents(id), candidate_commit TEXT NOT NULL, diff_hash TEXT REFERENCES artifacts(hash), notes_hash TEXT REFERENCES artifacts(hash), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_state(channel TEXT PRIMARY KEY, cursor TEXT, session_id TEXT, resume_url TEXT, activity_id TEXT);
CREATE TABLE IF NOT EXISTS discord_cursors(address TEXT PRIMARY KEY, message_id TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_routes(task_id TEXT PRIMARY KEY REFERENCES tasks(id), parent_task TEXT REFERENCES tasks(id), parent_channel TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS inbound_messages(channel TEXT NOT NULL, external_id TEXT NOT NULL, address TEXT NOT NULL, body_hash TEXT NOT NULL REFERENCES artifacts(hash), task_id TEXT REFERENCES tasks(id), received_at TEXT NOT NULL, PRIMARY KEY(channel,external_id));
CREATE TABLE IF NOT EXISTS delivery_retries(delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id), attempts INTEGER NOT NULL, retry_at REAL NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS route_retries(task_id TEXT PRIMARY KEY REFERENCES channel_routes(task_id), attempts INTEGER NOT NULL, retry_at REAL NOT NULL, reason TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search_items USING fts5(text,content='items',content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_search_insert AFTER INSERT ON items BEGIN INSERT INTO search_items(rowid,text) VALUES(new.id,new.text); END;

CREATE TABLE IF NOT EXISTS web_runs(operation_id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),agent_id TEXT NOT NULL REFERENCES agents(id),turn_id TEXT,kind TEXT NOT NULL,query TEXT NOT NULL,context_hash TEXT NOT NULL REFERENCES artifacts(hash),settings_hash TEXT NOT NULL REFERENCES artifacts(hash),status TEXT NOT NULL,discovery_hash TEXT REFERENCES artifacts(hash),review_call TEXT,review_hash TEXT REFERENCES artifacts(hash),result_hash TEXT REFERENCES artifacts(hash),created_at TEXT NOT NULL,accessed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS web_sources(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES web_runs(operation_id),rank INTEGER NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,manifest_hash TEXT REFERENCES artifacts(hash),created_at TEXT NOT NULL,UNIQUE(operation_id,rank));
CREATE TABLE IF NOT EXISTS web_files(path TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES web_sources(id),hash TEXT NOT NULL REFERENCES artifacts(hash),media_type TEXT NOT NULL,kind TEXT NOT NULL,accessed_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS web_files_hash ON web_files(hash);

-- Derived memory never replaces accepted lifecycle or original conversations.
CREATE TABLE IF NOT EXISTS memory_nodes(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),kind TEXT NOT NULL CHECK(kind IN ('day','episode','event')),task_id TEXT REFERENCES tasks(id),day TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,first_item INTEGER NOT NULL,last_item INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_edges(parent TEXT NOT NULL REFERENCES memory_nodes(id),child TEXT NOT NULL REFERENCES memory_nodes(id),PRIMARY KEY(parent,child));
CREATE TABLE IF NOT EXISTS memory_indexed(item_id INTEGER PRIMARY KEY REFERENCES items(id),node_id TEXT REFERENCES memory_nodes(id));
CREATE TABLE IF NOT EXISTS memory_fragments(node_id TEXT NOT NULL REFERENCES memory_nodes(id),part INTEGER NOT NULL,start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,text TEXT NOT NULL,batch_id TEXT,PRIMARY KEY(node_id,part));
CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(node_id UNINDEXED,agent_id UNINDEXED,text);
CREATE TABLE IF NOT EXISTS memory_heads(agent_id TEXT PRIMARY KEY REFERENCES agents(id),version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memory_facts(agent_id TEXT NOT NULL REFERENCES agents(id),kind TEXT NOT NULL CHECK(kind IN ('state','persona')),key TEXT NOT NULL,scope TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','resolved')),confidence TEXT NOT NULL CHECK(confidence IN ('explicit','inferred')),revision INTEGER NOT NULL,sources_json TEXT NOT NULL,quote TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(agent_id,kind,key));
CREATE TABLE IF NOT EXISTS memory_fact_revisions(agent_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,revision INTEGER NOT NULL,version INTEGER NOT NULL,record_json TEXT NOT NULL,batch_id TEXT,created_at TEXT NOT NULL,PRIMARY KEY(agent_id,kind,key,revision));
CREATE TABLE IF NOT EXISTS memory_batches(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),task_id TEXT REFERENCES tasks(id),request_hash TEXT NOT NULL REFERENCES artifacts(hash),call_id TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,watermark INTEGER NOT NULL,result_hash TEXT REFERENCES artifacts(hash),error TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_packets(item_id INTEGER PRIMARY KEY REFERENCES items(id),agent_id TEXT NOT NULL REFERENCES agents(id),version INTEGER NOT NULL,through_item INTEGER NOT NULL,source_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_retrievals(task_id TEXT PRIMARY KEY REFERENCES tasks(id),agent_id TEXT NOT NULL UNIQUE REFERENCES agents(id),parent_task TEXT NOT NULL REFERENCES tasks(id),owner_item INTEGER NOT NULL REFERENCES items(id),turn_id TEXT REFERENCES owner_turns(id),triaged INTEGER NOT NULL DEFAULT 0,report_item INTEGER REFERENCES items(id),deadline REAL NOT NULL,created_at TEXT NOT NULL,report_hash TEXT REFERENCES artifacts(hash));
CREATE TABLE IF NOT EXISTS memory_compactions(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),task_id TEXT NOT NULL REFERENCES tasks(id),status TEXT NOT NULL,context_id TEXT REFERENCES contexts(id),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_context_coverage(context_id TEXT NOT NULL REFERENCES contexts(id),item_id INTEGER NOT NULL REFERENCES items(id),PRIMARY KEY(context_id,item_id));
CREATE TABLE IF NOT EXISTS memory_missing(item_id INTEGER PRIMARY KEY REFERENCES items(id),error TEXT NOT NULL,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preset_invocations(operation_id TEXT PRIMARY KEY REFERENCES operations(id),task_id TEXT NOT NULL REFERENCES tasks(id),agent_id TEXT NOT NULL REFERENCES agents(id),path TEXT NOT NULL,template_hash TEXT NOT NULL REFERENCES artifacts(hash),bindings_hash TEXT NOT NULL REFERENCES artifacts(hash),request_hash TEXT NOT NULL REFERENCES artifacts(hash),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_launches(operation_id TEXT PRIMARY KEY REFERENCES operations(id),parent_task TEXT NOT NULL REFERENCES tasks(id),owner_item INTEGER NOT NULL REFERENCES items(id),kind TEXT NOT NULL,request_hash TEXT NOT NULL REFERENCES artifacts(hash),workers_json TEXT NOT NULL,report_hash TEXT REFERENCES artifacts(hash),report_item INTEGER REFERENCES items(id),obsolete INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_awaits(operation_id TEXT PRIMARY KEY REFERENCES operations(id),parent_task TEXT NOT NULL REFERENCES tasks(id),workers_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_dependencies(task_id TEXT PRIMARY KEY REFERENCES tasks(id),required INTEGER NOT NULL CHECK(required IN (0,1)));
CREATE TABLE IF NOT EXISTS worker_batches(operation_id TEXT PRIMARY KEY REFERENCES operations(id),parent_task TEXT NOT NULL REFERENCES tasks(id),owner_item INTEGER NOT NULL REFERENCES items(id),request_hash TEXT NOT NULL REFERENCES artifacts(hash),launches_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_reporting(task_id TEXT PRIMARY KEY REFERENCES tasks(id),start_turn INTEGER NOT NULL,deadline REAL NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_approvals(operation_id TEXT PRIMARY KEY REFERENCES operations(id),task_id TEXT NOT NULL REFERENCES tasks(id),call_id TEXT NOT NULL REFERENCES model_calls(id),payload_hash TEXT NOT NULL REFERENCES artifacts(hash),created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS working_compactions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),prior_context TEXT REFERENCES contexts(id),source_hash TEXT NOT NULL REFERENCES artifacts(hash),call_id TEXT NOT NULL,status TEXT NOT NULL,result_hash TEXT REFERENCES artifacts(hash),context_id TEXT REFERENCES contexts(id),error TEXT,created_at TEXT NOT NULL);

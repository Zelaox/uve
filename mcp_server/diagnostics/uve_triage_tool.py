"""Register the bounded index reader with the existing Lab tool registry."""
from .uve_triage_query import mcp_read_query
from .uve_triage_store import load_config, utc_now


def register_observability_tool(registry):
    policy = load_config().policy('adapter_ref')
    if not policy['mcp_query_enabled']:
        return 0
    description = 'Read indexed Lab summaries and traces with a configured row limit.'
    response = registry.register_tool(
        name=policy['mcp_query_tool_name'], description=description,
        module_path='mcp_server.diagnostics.uve_triage_query', function_name='mcp_read_query',
        parameters={
            'command': {'type': 'string', 'required': True, 'enum': policy['mcp_query_commands']},
            'identifier': {'type': 'string', 'required': True},
            'limit': {'type': 'integer', 'required': False, 'minimum': 1, 'maximum': policy['max_query_rows']},
        },
        return_type='Dict[str, Any]', author='uve_triage', tags=['uve', 'triage', 'read'],
        version='1.0.0', function_obj=mcp_read_query, visibility='public', agent_id='uve_triage',
        purpose=description, timestamp=utc_now(), operations=['read'], effect='evidence_read',
        result_kind='triage_observability',
    )
    return int(bool(response.get('success')))

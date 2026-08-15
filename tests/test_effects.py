from spotter.config import McpToolSemantics
from spotter.effects import classify, effect_event, external_effects
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent


def test_representative_reversibility_classes() -> None:
    read = classify("Bash", {"command": "git status --short"})
    local = classify("apply_patch", {"path": "src/app.py"})
    remote = classify("Bash", {"command": "git push origin feature"})
    assert (read.reversibility_class, read.kind) == ("A", "observation")
    assert (local.reversibility_class, local.reversible) == ("B", True)
    assert (remote.reversibility_class, remote.kind, remote.resource) == (
        "C",
        "git_remote_write",
        "origin",
    )


def test_external_tool_writes_are_conservatively_class_c() -> None:
    write = classify("mcp__github__create_issue", {"repository": "org/repo"})
    read = classify("mcp__github__list_issues", {"repository": "org/repo"})
    assert (write.reversibility_class, write.resource) == ("C", "org/repo")
    assert read.reversibility_class == "A"


def test_configured_mcp_semantics_are_exact_to_server_and_tool() -> None:
    semantics = (
        McpToolSemantics("inventory", "lookup", "read", "A", ("item_id", "url")),
        McpToolSemantics("admin", "lookup", "delete", "C", ("item_id",)),
    )

    read = classify(
        "mcp__inventory__lookup",
        {"item_id": 42, "url": "https://user:secret@example.com/items?token=secret"},
        semantics,
    )
    delete = classify("mcp__admin__lookup", {"item_id": 42}, semantics)
    unknown_server = classify("mcp__other__lookup", {"description": "safe read"}, semantics)

    assert (
        read.reversibility_class,
        read.classifier_id,
        read.semantic_operation,
        read.resource,
    ) == (
        "A",
        "mcp_config",
        "mcp.inventory.lookup.read",
        "item_id=42|url=https://example.com/items",
    )
    assert (delete.reversibility_class, delete.semantic_operation, delete.resource) == (
        "C",
        "mcp.admin.lookup.delete",
        "item_id=42",
    )
    assert (unknown_server.reversibility_class, unknown_server.reason_code) == (
        "C",
        "unknown_mcp_tool",
    )


def test_configured_mcp_class_b_remains_checkpoint_eligible() -> None:
    semantics = (McpToolSemantics("local", "write_file", "write", "B", ("path",)),)

    assessment = classify("mcp__local__write_file", {"path": "notes.txt"}, semantics)

    assert (
        assessment.reversibility_class,
        assessment.reversible,
        assessment.kind,
        assessment.reason_code,
    ) == ("B", True, "configured_tool_write", "configured_semantics")


def test_remote_cli_classification_uses_supported_operation_semantics() -> None:
    cases = {
        "gh pr view 42 --repo org/repo": ("A", "gh.pr.view", "org/repo"),
        "gh pr merge 42 --repo org/repo": ("C", "gh.pr.merge", "org/repo"),
        "gh api repos/org/repo/issues": (
            "A",
            "gh.api.get",
            "github:repos/org/repo/issues",
        ),
        "gh api -X PATCH repos/org/repo/issues/1": (
            "C",
            "gh.api.patch",
            "github:repos/org/repo/issues/1",
        ),
        "gh api -XPOST repos/org/repo/issues": (
            "C",
            "gh.api.post",
            "github:repos/org/repo/issues",
        ),
        "kubectl get pods -n prod": ("A", "kubectl.get", "kubernetes:namespace/prod:pods"),
        "kubectl apply -f deployment.yaml": (
            "C",
            "kubectl.apply",
            "kubernetes:manifest:deployment.yaml",
        ),
        "terraform validate": ("A", "terraform.validate", "terraform workspace"),
        "terraform apply plan.tfplan": ("C", "terraform.apply", "terraform workspace"),
    }

    for command, expected in cases.items():
        assessment = classify("Bash", {"command": command})
        assert (
            assessment.reversibility_class,
            assessment.semantic_operation,
            assessment.resource,
        ) == expected
        assert assessment.parse_confidence == "exact"
        assert assessment.reason_code == "recognized_semantics"


def test_bounded_wrappers_and_composition_preserve_the_strongest_effect() -> None:
    env_read = classify("Bash", {"command": "env FOO=bar gh pr list"})
    sudo_read = classify("Bash", {"command": "sudo kubectl get pods"})
    nested_write = classify("Bash", {"command": "bash -lc 'kubectl apply -f x.yaml'"})
    redirected = classify("Bash", {"command": "git status > status.txt"})
    compound_unknown = classify("Bash", {"command": "git status && mystery-command"})
    pipeline_write = classify("Bash", {"command": "cat payload.json | gh api -X POST /items"})

    assert (env_read.reversibility_class, env_read.parse_confidence) == ("A", "bounded")
    assert (sudo_read.reversibility_class, sudo_read.parse_confidence) == ("A", "bounded")
    assert (nested_write.reversibility_class, nested_write.parse_confidence) == ("C", "bounded")
    assert (redirected.reversibility_class, redirected.resource) == ("B", "status.txt")
    assert compound_unknown.reversibility_class == "C"
    assert compound_unknown.reason_code == "unclassified_command_effect"
    assert compound_unknown.parse_confidence == "unknown"
    assert pipeline_write.reversibility_class == "C"
    assert pipeline_write.semantic_operation == "gh.api.post"


def test_known_family_unknowns_and_uninspected_scripts_remain_explicit() -> None:
    unknown_kubectl = classify("Bash", {"command": "kubectl exec pod -- date"})
    unknown_terraform = classify("Bash", {"command": "terraform workspace select prod"})
    script = classify("Bash", {"command": "python deploy.py"})
    malformed = classify("Bash", {"command": "gh pr view 'unterminated"})
    too_deep = classify(
        "Bash",
        {"command": 'bash -lc "bash -lc \'bash -lc \\"gh pr list\\"\'"'},
    )

    for assessment in (unknown_kubectl, unknown_terraform, script, malformed, too_deep):
        assert assessment.reversibility_class == "C"
        assert assessment.parse_confidence == "unknown"
        assert assessment.kind == "unknown_command_effect"

    assert unknown_kubectl.reason_code == "unsupported_kubectl_subcommand"
    assert unknown_terraform.reason_code == "unsupported_terraform_subcommand"
    assert script.reason_code == "uninspected_script"
    assert malformed.reason_code == "malformed_shell"
    assert too_deep.reason_code == "wrapper_depth_exceeded"


def test_terraform_plan_output_is_a_local_mutation() -> None:
    assessment = classify("Bash", {"command": "terraform plan -out=plan.tfplan"})
    formatted = classify("Bash", {"command": "terraform fmt"})
    checked = classify("Bash", {"command": "terraform fmt -check"})

    assert assessment.reversibility_class == "B"
    assert assessment.resource == "plan.tfplan"
    assert assessment.semantic_operation == "terraform.plan.out"
    assert (formatted.reversibility_class, formatted.semantic_operation) == (
        "B",
        "terraform.fmt",
    )
    assert (checked.reversibility_class, checked.semantic_operation) == (
        "A",
        "terraform.fmt.check",
    )


def test_malformed_shell_composition_and_gh_api_fields_are_not_read_only() -> None:
    malformed = classify("Bash", {"command": "git status &&"})
    api_field = classify("Bash", {"command": "gh api repos/org/repo/issues -f title=bug"})
    sed_in_place = classify("Bash", {"command": "sed -i.bak s/old/new/ config.toml"})
    too_large = classify("Bash", {"command": "echo " + ("x" * 4096)})

    assert malformed.reason_code == "malformed_composition"
    assert malformed.parse_confidence == "unknown"
    assert api_field.reversibility_class == "C"
    assert api_field.semantic_operation == "gh.api.post"
    assert sed_in_place.reversibility_class == "B"
    assert sed_in_place.semantic_operation == "sed.in_place"
    assert too_large.reason_code == "command_too_large"


def test_curl_classification_uses_http_method_and_redacts_resource_secrets() -> None:
    read = classify(
        "Bash",
        {"command": "curl 'https://user:secret@example.com/items?token=secret#frag'"},
    )
    post = classify(
        "Bash",
        {"command": 'curl -d \'{"name":"item"}\' https://api.example.com/items'},
    )
    attached = classify(
        "Bash",
        {"command": "curl -XDELETE https://api.example.com/items/1"},
    )
    download = classify(
        "Bash",
        {"command": "curl -o result.json https://api.example.com/items"},
    )
    cookie_jar = classify(
        "Bash",
        {"command": "curl -c cookies.txt https://api.example.com/items"},
    )
    remote_name = classify(
        "Bash",
        {"command": "curl --remote-name https://api.example.com/items.json"},
    )

    assert (read.reversibility_class, read.semantic_operation, read.resource) == (
        "A",
        "http.get",
        "https://example.com/items",
    )
    assert (post.reversibility_class, post.semantic_operation) == ("C", "http.post")
    assert (attached.reversibility_class, attached.semantic_operation) == (
        "C",
        "http.delete",
    )
    assert (download.reversibility_class, download.semantic_operation) == (
        "B",
        "http.get.download",
    )
    assert download.resource == "result.json <- https://api.example.com/items"
    assert cookie_jar.reversibility_class == "B"
    assert remote_name.reversibility_class == "B"


def test_curl_unknown_shapes_never_inherit_get_semantics() -> None:
    config = classify(
        "Bash",
        {"command": "curl -Krequest.conf https://api.example.com/items"},
    )
    unknown_method = classify(
        "Bash",
        {"command": "curl -XPROPFIND https://api.example.com/items"},
    )
    missing_url = classify("Bash", {"command": "curl example.com/items"})
    get_with_body = classify(
        "Bash",
        {"command": "curl -XGET -d payload https://api.example.com/items"},
    )

    assert config.reason_code == "unsupported_curl_shape"
    assert unknown_method.reason_code == "unsupported_http_method"
    assert missing_url.reason_code == "missing_http_resource"
    assert get_with_body.reason_code == "http_read_method_with_request_body"
    assert all(
        assessment.reversibility_class == "C" and assessment.parse_confidence == "unknown"
        for assessment in (config, unknown_method, missing_url, get_with_body)
    )


def test_curl_clustered_short_options_preserve_methods_and_local_outputs() -> None:
    download = classify(
        "Bash",
        {"command": "curl -so result.json https://api.example.com/items"},
    )
    post = classify(
        "Bash",
        {"command": "curl -sXPOST https://api.example.com/items"},
    )
    data = classify(
        "Bash",
        {"command": "curl -sd payload https://api.example.com/items"},
    )
    config = classify(
        "Bash",
        {"command": "curl -sKrequest.conf https://api.example.com/items"},
    )
    data_with_flag_letters = classify(
        "Bash",
        {"command": "curl -dsize=BIG https://api.example.com/items"},
    )
    unknown_cluster = classify(
        "Bash",
        {"command": "curl -s@ https://api.example.com/items"},
    )

    assert (download.reversibility_class, download.resource) == (
        "B",
        "result.json <- https://api.example.com/items",
    )
    assert (post.reversibility_class, post.semantic_operation) == ("C", "http.post")
    assert (data.reversibility_class, data.semantic_operation) == ("C", "http.post")
    assert (
        data_with_flag_letters.reversibility_class,
        data_with_flag_letters.semantic_operation,
    ) == ("C", "http.post")
    assert config.reason_code == "unsupported_curl_shape"
    assert unknown_cluster.reason_code == "unsupported_curl_cluster"


def test_invalid_url_port_fallback_still_redacts_credentials() -> None:
    assessment = classify(
        "Bash",
        {"command": ("curl 'https://user:secret@example.com:99999/items?token=secret#fragment'")},
    )

    assert assessment.reversibility_class == "A"
    assert assessment.resource == "https://example.com:99999/items"


def test_database_cli_classification_limits_reads_to_bounded_metadata_queries() -> None:
    postgres_list = classify(
        "Bash",
        {"command": "psql --dbname=postgresql://user:secret@db.example.com/app --list"},
    )
    mysql_show = classify(
        "Bash",
        {"command": "mysql -h db.example.com -D app -e 'SHOW TABLES'"},
    )
    postgres_delete = classify(
        "Bash",
        {"command": "psql -h db.example.com -d app -c 'DELETE FROM jobs'"},
    )
    mysql_update = classify(
        "Bash",
        {"command": "mysql -Dapp -eUPDATE\\ jobs\\ SET\\ status=1"},
    )

    assert (postgres_list.reversibility_class, postgres_list.resource) == (
        "A",
        "postgresql://db.example.com/app",
    )
    assert (mysql_show.reversibility_class, mysql_show.semantic_operation) == (
        "A",
        "mysql.show",
    )
    assert mysql_show.resource == "mysql:host/db.example.com:database/app"
    assert (postgres_delete.reversibility_class, postgres_delete.semantic_operation) == (
        "C",
        "postgres.delete",
    )
    assert (mysql_update.reversibility_class, mysql_update.semantic_operation) == (
        "C",
        "mysql.update",
    )


def test_database_scripts_selects_and_multi_statements_remain_unknown() -> None:
    script = classify("Bash", {"command": "psql -f migration.sql"})
    select = classify("Bash", {"command": "psql -c 'SELECT run_user_function()'"})
    multi = classify("Bash", {"command": "mysql -e 'SHOW TABLES; DELETE FROM jobs'"})
    multiple_options = classify(
        "Bash",
        {"command": "psql -c 'SHOW search_path' -c 'DELETE FROM jobs'"},
    )

    assert script.reason_code == "uninspected_database_script"
    assert select.reason_code == "unsupported_sql_semantics"
    assert multi.reason_code == "unsupported_sql_shape"
    assert multiple_options.reason_code == "multiple_database_commands"
    assert all(
        assessment.reversibility_class == "C" and assessment.parse_confidence == "unknown"
        for assessment in (script, select, multi, multiple_options)
    )


def test_class_c_result_becomes_effect_with_recovery_identity() -> None:
    result = TraceEvent(
        "tool_result",
        {
            "reversibility_class": "C",
            "effect_kind": "git_remote_write",
            "resource": "origin",
            "tool_response": {"exit_code": 0},
            "checkpoint": "abc123",
            "turn_id": "turn-2",
            "tool_use_id": "call-7",
            "effect_classifier": "git",
            "effect_reason": "recognized_semantics",
            "effect_confidence": "exact",
            "semantic_operation": "git.push",
        },
    )
    effect = effect_event(result)
    assert effect is not None
    assert effect.payload == {
        "kind": "git_remote_write",
        "resource": "origin",
        "result": "succeeded",
        "reversible": False,
        "checkpoint": "abc123",
        "turn_id": "turn-2",
        "tool_use_id": "call-7",
        "classifier": "git",
        "reason": "recognized_semantics",
        "confidence": "exact",
        "semantic_operation": "git.push",
    }
    records = [StepRecord(4, effect, "abc123")]
    assert external_effects(records) == [effect.payload]
    assert external_effects(records, through_step=3) == []

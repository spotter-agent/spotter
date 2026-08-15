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

import pytest
import yaml
from reproagent_agent.codegen import (
    classify_error,
    generate_code,
    generate_from_plan,
    repair_bundle,
    validate_bundle,
)
from reproagent_agent.planning import ScalingConfig, build_plan
from reproagent_schemas import (
    ClassifyRequest,
    CodeBundle,
    CodeFile,
    ErrorCategory,
    EvidenceField,
    EvidenceStatus,
    RepairRequest,
    ReproductionCard,
    RunStatus,
    compute_code_hash,
)


def explicit(value, page=8):
    return EvidenceField(
        value=value,
        status=EvidenceStatus.PAPER_EXPLICIT,
        evidence_quote=f"...{value}...",
        source_page=page,
    )


def make_card() -> ReproductionCard:
    return ReproductionCard(
        method=explicit("LeNet-5 gradient-based learning"),
        model_architecture=explicit("LeNet-5 convolutional network"),
        dataset=explicit("MNIST handwritten digits"),
        optimizer=explicit("SGD"),
        loss_function=explicit("cross-entropy"),
        hyperparameters={"learning_rate": explicit("0.001")},
        evaluation_metrics=[explicit("accuracy")],
    )


def make_plan(**scaling) -> object:
    return build_plan(make_card(), ScalingConfig(**scaling))


def replace_file(bundle: CodeBundle, path: str, content: str) -> CodeBundle:
    files = [
        CodeFile(path=f.path, content=content if f.path == path else f.content)
        for f in bundle.files
    ]
    return CodeBundle(files=files, entrypoint=bundle.entrypoint, requirements=bundle.requirements)


class TestGenerator:
    def test_produces_expected_files(self):
        bundle = generate_code(make_card(), make_plan())
        assert {f.path for f in bundle.files} == {
            "model.py",
            "train.py",
            "evaluate.py",
            "config.yaml",
            "requirements.txt",
        }
        assert bundle.entrypoint == "train.py"

    def test_code_hash_is_deterministic(self):
        plan = make_plan()
        a = generate_from_plan(plan)
        b = generate_from_plan(plan)
        assert a.code_hash == b.code_hash == compute_code_hash(a.files)
        assert a.code_hash

    def test_plan_change_changes_hash(self):
        a = generate_from_plan(make_plan(epochs=3))
        b = generate_from_plan(make_plan(epochs=5))
        assert a.code_hash != b.code_hash

    def test_config_reflects_plan(self):
        plan = make_plan(epochs=5, batch_size=32, train_subset_size=2000)
        bundle = generate_from_plan(plan)
        config = yaml.safe_load(next(f.content for f in bundle.files if f.path == "config.yaml"))
        assert config["training"]["epochs"] == 5
        assert config["training"]["batch_size"] == 32
        assert config["data"]["train_subset_size"] == 2000
        assert config["data"]["dir"] == "/data/mnist"
        assert config["output"]["metrics_filename"] == "metrics.json"

    def test_unsupported_optimizer_fails_at_generation(self):
        plan = make_plan()
        opt = plan.training_config.optimizer.model_copy(
            update={"value": "stochastic diagonal Levenberg-Marquardt procedure"}
        )
        training = plan.training_config.model_copy(update={"optimizer": opt})
        plan = plan.model_copy(update={"training_config": training})
        with pytest.raises(ValueError, match="unsupported optimizer"):
            generate_from_plan(plan)

    def test_unsupported_loss_fails_at_generation(self):
        plan = make_plan()
        loss = plan.training_config.loss_function.model_copy(update={"value": "nll"})
        training = plan.training_config.model_copy(update={"loss_function": loss})
        plan = plan.model_copy(update={"training_config": training})
        with pytest.raises(ValueError, match="unsupported loss_function"):
            generate_from_plan(plan)

    def test_loss_alias_normalized_to_cross_entropy(self):
        plan = make_plan()
        loss = plan.training_config.loss_function.model_copy(update={"value": "softmax loss"})
        training = plan.training_config.model_copy(update={"loss_function": loss})
        plan = plan.model_copy(update={"training_config": training})
        bundle = generate_from_plan(plan)
        config = yaml.safe_load(next(f.content for f in bundle.files if f.path == "config.yaml"))
        assert config["training"]["loss_function"] == "cross_entropy"

    def test_generated_bundle_validates(self):
        report = validate_bundle(generate_from_plan(make_plan()))
        assert report.ok, report.issues


class TestValidator:
    def test_syntax_error_detected(self):
        bundle = replace_file(generate_from_plan(make_plan()), "model.py", "def broken(:\n")
        report = validate_bundle(bundle)
        assert not report.ok
        assert any(i.code == "syntax_error" for i in report.issues)

    def test_banned_import_os(self):
        bundle = replace_file(generate_from_plan(make_plan()), "model.py", "import os\n")
        assert any(i.code == "banned_import" for i in validate_bundle(bundle).issues)

    def test_banned_import_subprocess(self):
        bundle = replace_file(
            generate_from_plan(make_plan()), "model.py", "import subprocess\n"
        )
        assert any(i.code == "banned_import" for i in validate_bundle(bundle).issues)

    def test_banned_env_attribute(self):
        bundle = replace_file(
            generate_from_plan(make_plan()), "model.py", "import torch\nx = torch.environ\n"
        )
        assert any(i.code == "banned_attr" for i in validate_bundle(bundle).issues)

    def test_banned_eval_call(self):
        bundle = replace_file(
            generate_from_plan(make_plan()), "model.py", "eval('1+1')\n"
        )
        assert any(i.code == "banned_call" for i in validate_bundle(bundle).issues)

    def test_network_download_forbidden(self):
        bundle = replace_file(
            generate_from_plan(make_plan()),
            "train.py",
            "from torchvision import datasets\ndatasets.MNIST('/data', download=True)\n",
        )
        assert any(i.code == "network_download" for i in validate_bundle(bundle).issues)

    def test_torch_hub_forbidden(self):
        bundle = replace_file(
            generate_from_plan(make_plan()),
            "model.py",
            "import torch\ntorch.hub.load('repo', 'model')\n",
        )
        assert any(i.code == "banned_attr" for i in validate_bundle(bundle).issues)

    def test_sensitive_path_forbidden(self):
        bundle = replace_file(
            generate_from_plan(make_plan()),
            "model.py",
            "from pathlib import Path\nPath('/etc/passwd').read_text()\n",
        )
        assert any(i.code == "sensitive_path" for i in validate_bundle(bundle).issues)

    def test_missing_config_key(self):
        bundle = replace_file(generate_from_plan(make_plan()), "config.yaml", "seed: 42\n")
        report = validate_bundle(bundle)
        assert any(i.code == "missing_config_key" for i in report.issues)

    def test_missing_entrypoint(self):
        bundle = generate_from_plan(make_plan())
        files = [f for f in bundle.files if f.path != "train.py"]
        broken = CodeBundle.model_construct(
            files=files, entrypoint="train.py", requirements=bundle.requirements
        )
        assert any(i.code == "missing_entrypoint" for i in validate_bundle(broken).issues)

    def test_non_whitelisted_dependency(self):
        bundle = generate_from_plan(make_plan())
        broken = CodeBundle.model_construct(
            files=bundle.files, entrypoint="train.py", requirements=["requests==2.0.0"]
        )
        assert any(
            i.code == "dependency_not_whitelisted" for i in validate_bundle(broken).issues
        )

    def test_missing_metric_key(self):
        bundle = replace_file(
            generate_from_plan(make_plan()), "train.py", "print('no metrics here')\n"
        )
        assert any(i.code == "missing_metric_key" for i in validate_bundle(bundle).issues)


class TestClassify:
    def test_timeout_status(self):
        result = classify_error(ClassifyRequest(status=RunStatus.TIMEOUT))
        assert result.category is ErrorCategory.TIMEOUT

    def test_exit_137_is_oom(self):
        result = classify_error(ClassifyRequest(exit_code=137))
        assert result.category is ErrorCategory.OUT_OF_MEMORY

    @pytest.mark.parametrize(
        ("stderr", "category"),
        [
            ("SyntaxError: invalid syntax", ErrorCategory.SYNTAX_ERROR),
            ("ModuleNotFoundError: No module named 'foo'", ErrorCategory.IMPORT_ERROR),
            ("CUDA out of memory. Tried to allocate", ErrorCategory.OUT_OF_MEMORY),
            ("RuntimeError: size mismatch for weight", ErrorCategory.SHAPE_MISMATCH),
            ("Failed to download dataset", ErrorCategory.DATA_DOWNLOAD_ERROR),
            ("KeyError: 'metric_value' in metrics.json", ErrorCategory.METRIC_ERROR),
        ],
    )
    def test_stderr_signatures(self, stderr, category):
        result = classify_error(ClassifyRequest(stderr=stderr, exit_code=1))
        assert result.category is category

    def test_unknown_not_repairable(self):
        result = classify_error(ClassifyRequest(stderr="something weird", exit_code=1))
        assert result.category is ErrorCategory.UNKNOWN_ERROR
        assert not result.repairable


class TestRepair:
    def test_oom_requires_replan(self):
        bundle = generate_from_plan(make_plan())
        classification = classify_error(ClassifyRequest(exit_code=137))
        response = repair_bundle(
            RepairRequest(bundle=bundle, classification=classification, plan=make_plan())
        )
        assert response.replan_required
        assert response.bundle.code_hash == bundle.code_hash

    def test_syntax_error_repaired_to_valid_bundle(self):
        plan = make_plan()
        broken = replace_file(generate_from_plan(plan), "model.py", "def broken(:\n")
        classification = classify_error(ClassifyRequest(stderr="SyntaxError: invalid syntax"))
        response = repair_bundle(
            RepairRequest(bundle=broken, classification=classification, plan=plan)
        )
        assert not response.replan_required
        assert response.bundle.code_hash != broken.code_hash
        assert validate_bundle(response.bundle).ok
        assert response.change_reason

    def test_unknown_left_unchanged(self):
        bundle = generate_from_plan(make_plan())
        classification = classify_error(ClassifyRequest(stderr="weird", exit_code=1))
        response = repair_bundle(
            RepairRequest(bundle=bundle, classification=classification, plan=make_plan())
        )
        assert not response.replan_required
        assert response.bundle.code_hash == bundle.code_hash
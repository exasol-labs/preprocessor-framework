"""Module ecosystem package: contract parsing/validation, registry, CLI, lifecycle.

Submodules:

* ``manifest`` — parse/validate ``module.toml`` and verify artifacts (contract).
* ``registry`` — read/generate the federated index and fetch artifacts.
* ``deploy`` — deploy a module version (artifact + RULE_ENSURE + MODULE_REGISTER).
* ``offline_bundle`` — emit a self-contained offline ``.sql`` (air-gap path).
* ``sync`` — declarative, idempotent convergence, plus the update/remove flows.
* ``validate`` — static contract checks and a live scratch-deploy smoke test.
* ``commands`` — the ``preproc module`` CLI command group.
"""

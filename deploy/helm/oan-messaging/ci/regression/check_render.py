# ruff: noqa: S603, S607
# Subprocess argv is constructed solely from fixed local regression fixtures; no shell/user input.
"""Render-level regression checks for the oan-messaging chart (MSG-08H review).

Needs `helm` on PATH and PyYAML. Contacts no cluster. Exit status 0 = all checks passed.

    python3 ci/regression/check_render.py          (from the chart directory)

Covers:
* names: uniqueness by kind/name across hook AND regular resources, Kubernetes name
  limits, component suffixes kept, for long release names, nameOverride and
  fullnameOverride; over-long or invalid names are refused (P2 truncation);
* wiring: every Service/PDB/NetworkPolicy selector matches exactly the intended
  component's pods; private keys only in the snapshot init container; the init container
  embeds files/tls_snapshot.py byte for byte; runtime paths use the snapshot's current/;
* connection estimates: exact steady/rollout figures, exact budget boundaries, invalid
  inputs refused (P2 budget);
* migration network modes: hook policy ordering and selectors, mode validation (P2 hook);
* every earlier render guard.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2]
CI = CHART / "ci"
ALL = CI / "02-all-components-proposed-values.yaml"
SCRIPT = (CHART / "files" / "tls_snapshot.py").read_text()

failures: list[str] = []
passed = 0


def check(cond: bool, label: str) -> None:
    global passed
    if cond:
        passed += 1
    else:
        failures.append(label)
        print(f"FAIL  {label}")


def helm(*args: str, release: str = "oan-msg") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", release, str(CHART), "-n", "oan-messaging-dev", *args],
        capture_output=True,
        text=True,
    )


def render(*args: str, release: str = "oan-msg") -> list[dict[str, Any]]:
    r = helm(*args, release=release)
    if r.returncode != 0:
        raise AssertionError(f"render failed for {args}: {r.stderr.strip()}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def error(*args: str, release: str = "oan-msg") -> str:
    r = helm(*args, release=release)
    return "" if r.returncode == 0 else r.stderr


def pod_spec(d: dict[str, Any]) -> dict[str, Any] | None:
    if d["kind"] in ("Deployment", "Job"):
        return d["spec"]["template"]["spec"]
    if d["kind"] == "CronJob":
        return d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return None


def pod_labels(d: dict[str, Any]) -> dict[str, str]:
    if d["kind"] == "CronJob":
        return d["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    return d["spec"]["template"]["metadata"]["labels"]


def matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        if expr["operator"] == "In" and labels.get(expr["key"]) not in expr["values"]:
            return False
    return True


LIMIT = {"Service": 63, "CronJob": 52}


# ---------------------------------------------------------------- structural checks


def structural(docs: list[dict[str, Any]], label: str) -> None:
    names = Counter((d["kind"], d["metadata"]["name"]) for d in docs)
    dupes = [k for k, n in names.items() if n > 1]
    check(not dupes, f"{label}: unique kind/name (dupes: {dupes})")
    for d in docs:
        name = d["metadata"]["name"]
        check(len(name) <= LIMIT.get(d["kind"], 253), f"{label}: {d['kind']} {name} length")
        check(
            bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)),
            f"{label}: {d['kind']} {name} is DNS-1123",
        )
        for k, v in (d["metadata"].get("labels") or {}).items():
            check(len(str(v)) <= 63, f"{label}: label {k} value length on {name}")

    workloads = [d for d in docs if pod_spec(d) is not None]
    for d in workloads:
        comp = d["metadata"]["labels"]["app.kubernetes.io/component"]
        check(
            d["metadata"]["name"].endswith("-" + comp),
            f"{label}: {d['kind']} {d['metadata']['name']} keeps its component suffix",
        )

    def selected(sel: dict[str, Any]) -> list[str]:
        return sorted(
            pod_labels(w)["app.kubernetes.io/component"]
            for w in workloads
            if matches(sel, pod_labels(w))
        )

    for d in docs:
        comp = (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component")
        if d["kind"] == "Service":
            check(
                selected({"matchLabels": d["spec"]["selector"]}) == [comp],
                f"{label}: Service {d['metadata']['name']} selects only {comp}",
            )
        if d["kind"] == "PodDisruptionBudget":
            check(
                selected(d["spec"]["selector"]) == [comp],
                f"{label}: PDB {d['metadata']['name']} selects only {comp}",
            )
        if d["kind"] == "NetworkPolicy" and comp not in ("internal",):
            check(
                selected(d["spec"]["podSelector"]) == [comp],
                f"{label}: NetworkPolicy {d['metadata']['name']} selects only {comp}",
            )
        if d["kind"] == "Deployment":
            check(
                matches(d["spec"]["selector"], pod_labels(d)),
                f"{label}: Deployment {d['metadata']['name']} selector matches its pods",
            )

    for d in workloads:
        spec = pod_spec(d)
        assert spec is not None
        key_vols = {
            v["name"]
            for v in spec.get("volumes", [])
            if "secret" in v and any(i["key"] == "tls.key" for i in v["secret"].get("items", []))
        }
        for c in spec["containers"]:
            mounted = {m["name"] for m in c.get("volumeMounts", [])}
            check(
                not (mounted & key_vols),
                f"{label}: {d['metadata']['name']}/{c['name']} never mounts a private-key Secret",
            )
            for e in c.get("env", []):
                if e["name"].startswith("OAN_MSG_SMS8028_CLIENT_") and "value" in e:
                    check(
                        e["value"].startswith("/run/oan-msg/tls/current/"),
                        f"{label}: {e['name']} reads the published snapshot",
                    )
        for ic in spec.get("initContainers", []):
            check(
                ic["command"][:3] == ["python", "-I", "-c"] and ic["command"][3] == SCRIPT,
                f"{label}: {d['metadata']['name']} init embeds files/tls_snapshot.py verbatim",
            )


# ------------------------------------------------------------------- name matrix


def names() -> None:
    long_release = "r" * 53  # Helm's maximum release name length
    ok_cases: list[tuple[str, str, tuple[str, ...]]] = [
        ("default release", "oan-msg", ()),
        (
            "53-char release + short fullnameOverride",
            long_release,
            ("--set", "fullnameOverride=oan-msg-dev"),
        ),
        ("nameOverride", "oan-msg", ("--set", "nameOverride=oanm")),
        ("release containing chart name", "oan-messaging", ()),
        ("39-char fullnameOverride", "oan-msg", ("--set", "fullnameOverride=" + "x" * 39)),
        (
            "63-char nameOverride with fullnameOverride",
            "oan-msg",
            ("--set", "nameOverride=" + "n" * 63, "--set", "fullnameOverride=oan-msg-dev"),
        ),
    ]
    for label, release, extra in ok_cases:
        for values in sorted(CI.glob("*-values.yaml")):
            docs = render("-f", str(values), *extra, release=release)
            structural(docs, f"{label} [{values.name}]")
    refused = [
        (
            "40-char fullnameOverride",
            "oan-msg",
            ("--set", "fullnameOverride=" + "x" * 40),
            "at most 39",
        ),
        (
            "63-char fullnameOverride (review repro)",
            "oan-msg",
            ("--set", "fullnameOverride=" + "x" * 63),
            "at most 39",
        ),
        ("long release without override", "r" * 30, (), "at most 39"),
        (
            "upper-case fullnameOverride",
            "oan-msg",
            ("--set", "fullnameOverride=OAN-msg"),
            "DNS-1123",
        ),
        (
            "64-char nameOverride",
            "oan-msg",
            ("--set", "nameOverride=" + "n" * 64, "--set", "fullnameOverride=oan-msg-dev"),
            "at most 63 characters",
        ),
        (
            "64-char nameOverride without fullnameOverride",
            "oan-msg",
            ("--set", "nameOverride=" + "n" * 64),
            "at most 39",
        ),
    ]
    for label, release, extra, needle in refused:
        err = error("-f", str(ALL), *extra, release=release)
        check(needle in err, f"names refused: {label} -> {needle!r} (got {err.strip()[:120]!r})")


# --------------------------------------------------------------- connection budget


def per_pod(v: dict[str, int], worker: bool = False) -> int:
    return v["size"] + v["maxOverflow"] + (2 if worker else 0)


def budget() -> None:
    def figures(*extra: str) -> tuple[int, int]:
        err = error("-f", str(ALL), "--set", "database.connectionBudget=1", *extra)
        m = re.search(r"rollout \((\d+); steady state (\d+)\)", err)
        assert m, err
        return int(m.group(1)), int(m.group(2))

    # Independent expectation for ci/02: api 2x(5+5), worker 2x(5+5+2), receipts 2x(2+2),
    # cleanup cronjob 1x(2+0), migration 1; surge 1 each; terminating pods = replicas.
    api, wrk, rec, cln = 10, 12, 4, 2
    steady = 2 * api + 2 * wrk + 2 * rec + cln
    rollout = (2 * 2 + 1) * api + (2 * 2 + 1) * wrk + (2 * 2 + 1) * rec + cln + 1
    check(figures() == (rollout, steady), f"budget figures ci/02 == ({rollout}, {steady})")
    check(
        not error("-f", str(ALL), "--set", f"database.connectionBudget={rollout}"),
        f"budget exactly {rollout} renders",
    )
    check(
        "exceed database.connectionBudget"
        in error("-f", str(ALL), "--set", f"database.connectionBudget={rollout - 1}"),
        f"budget {rollout - 1} is refused",
    )
    check(
        figures("--set", "api.rollout.maxSurge=2") == (rollout + api, steady),
        "api maxSurge 2 adds one api pod to the rollout estimate only",
    )
    check(
        figures("--set", "worker.replicas=3") == (rollout + 2 * wrk, steady + wrk),
        "worker replicas 3 adds 2 terminating/new pods to rollout, 1 to steady",
    )
    check(
        figures("--set", "cleanup.mode=deployment") == (rollout, steady),
        "cleanup Deployment (Recreate) counts one pod, like the CronJob",
    )
    check(
        figures("--set", "migration.enabled=false") == (rollout - 1, steady),
        "migration hook adds exactly 1 to the rollout estimate",
    )
    deploy = render("-f", str(ALL), "--set", "cleanup.mode=deployment")
    strategy = [
        d["spec"]["strategy"]["type"]
        for d in deploy
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-cleanup")
    ]
    check(strategy == ["Recreate"], "cleanup Deployment uses Recreate")
    surges = {
        d["metadata"]["name"].rsplit("-", 1)[-1]: d["spec"]["strategy"]["rollingUpdate"]
        for d in render("-f", str(ALL), "--set", "worker.rollout.maxSurge=0")
        if d["kind"] == "Deployment" and "rollingUpdate" in d["spec"]["strategy"]
    }
    check(
        surges["worker"]["maxSurge"] == 0 and surges["api"]["maxSurge"] == 1,
        "rendered maxSurge follows rollout.maxSurge",
    )

    bad_inputs = [
        ("api.replicas=-1", ">= 0"),
        ("worker.replicas=-3", ">= 0"),
        ("api.pool.size=0", ">= 1"),
        ("receipts.pool.maxOverflow=-1", ">= 0"),
        ("worker.coordinationConnections=0", ">= 1"),
        ("database.connectionBudget=-5", ">= 0"),
        ("api.rollout.maxSurge=-1", ">= 0"),
        ("cleanup.pool.size=-2", ">= 1"),
    ]
    for setting, needle in bad_inputs:
        err = error("-f", str(ALL), "--set", setting)
        check(needle in err, f"invalid input refused: {setting}")
    err = error("-f", str(ALL), "--set-json", '{"api":{"replicas":1.5}}')
    check("must be an integer" in err, "fractional replicas refused")


# --------------------------------------------------------------- migration network


def migration() -> None:
    def hook_policy(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            d
            for d in docs
            if d["kind"] == "NetworkPolicy"
            and d["metadata"].get("annotations", {}).get("helm.sh/hook")
        ]

    docs = render("-f", str(ALL))
    hp = hook_policy(docs)
    check(len(hp) == 1, "hook mode renders one migration hook NetworkPolicy")
    if hp:
        ann = hp[0]["metadata"]["annotations"]
        weights = {
            d["kind"]: int(d["metadata"]["annotations"]["helm.sh/hook-weight"])
            for d in docs
            if d["metadata"].get("annotations", {}).get("helm.sh/hook")
        }
        check(
            weights["ServiceAccount"] < weights["NetworkPolicy"] < weights["Job"],
            f"hook order SA < NetworkPolicy < Job ({weights})",
        )
        check(
            ann["helm.sh/hook"] == "pre-install,pre-upgrade", "policy is a pre-install/upgrade hook"
        )
        job = next(d for d in docs if d["kind"] == "Job")
        check(
            matches(hp[0]["spec"]["podSelector"], job["spec"]["template"]["metadata"]["labels"]),
            "hook policy selects the migration pod",
        )
        egress = hp[0]["spec"]["egress"]
        cidrs = [b["ipBlock"]["cidr"] for r in egress for b in r["to"] if "ipBlock" in b]
        check(cidrs == ["10.0.20.0/24"], "hook policy allows exactly the PostgreSQL CIDRs")
        check(hp[0]["spec"]["ingress"] == [], "hook policy denies ingress")
        envs = {
            e["name"]
            for c in job["spec"]["template"]["spec"]["containers"]
            for e in c.get("env", [])
        }
        check(
            envs == {"OAN_MSG_DATABASE_URL", "PYTHONDONTWRITEBYTECODE"},
            "migration receives only the migration database URL",
        )
        check(
            job["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"][
                "name"
            ]
            == "oan-messaging-database-owner",
            "migration uses the separate schema-owner Secret",
        )
    pre = render(
        "-f",
        str(ALL),
        "--set",
        "migration.network.mode=preprovisioned",
        "--set",
        "migration.network.preprovisionedPolicy=ops-allow-oan-migration",
    )
    check(not hook_policy(pre), "preprovisioned mode renders no hook policy")
    job = next(d for d in pre if d["kind"] == "Job")
    check(
        job["metadata"]["annotations"].get("oan.coss/migration-network-policy")
        == "ops-allow-oan-migration",
        "preprovisioned policy name recorded on the Job",
    )
    refused = [
        (("--set", "migration.network.mode=unrestricted"), "contradicts networkPolicy.egress"),
        (
            ("--set", "migration.network.mode=preprovisioned"),
            "requires migration.network.preprovisionedPolicy",
        ),
        (
            ("--set", "networkPolicy.egress.postgres.cidrs=null"),
            "requires networkPolicy.egress.postgres.cidrs",
        ),
        (("--set", "networkPolicy.enabled=false"), "requires networkPolicy.enabled=true"),
        (("--set", "migration.network.mode=open"), "must be hook, preprovisioned or unrestricted"),
    ]
    for extra, needle in refused:
        check(
            needle in error("-f", str(ALL), *extra), f"migration mode refused: {extra} -> {needle}"
        )
    check(not error(), "defaults (unrestricted, egress off) render")


# ------------------------------------------------------------ earlier render guards


def guards() -> None:
    a = ("-f", str(ALL))
    cases: list[tuple[tuple[str, ...], str]] = [
        (("--set", "worker.enabled=true"), "worker.command is required"),
        (("--set", "receipts.enabled=true"), "receipts.command is required"),
        (("--set", "cleanup.enabled=true"), "cleanup.command is required"),
        (
            (
                *a,
                "--set-string",
                r"receipts.service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-ssl-cert=x",
            ),
            "terminate or re-encrypt TLS",
        ),
        (
            (
                *a,
                "--set-string",
                r"receipts.service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-backend-protocol=http",
            ),
            "terminate or re-encrypt TLS",
        ),
        ((*a, "--set", "worker.terminationGracePeriodSeconds=60"), "must be >= the worker lease"),
        ((*a, "--set", "config.policy.workerLease=300"), "must be >= the worker lease"),
        (("--set", "config.policy.dedupWindw=10"), "is not a ServicePolicy field"),
        (("--set", "config.policy.dedupWindow=0"), "must be positive"),
        ((*a, "--set", "secrets.telegram.bots.Bad_Key=x"), "invalid bot key"),
        (
            (*a, "--set", "secrets.telegram.existingSecret="),
            "secrets.telegram.existingSecret is required",
        ),
        ((*a, "--set", "secrets.receipts.pinnedCertKeys=null"), "pinned 8028 certificate"),
        (
            (*a, "--set", "secrets.receipts.clientCaKeys={ca.crt}"),
            "secrets.receipts.clientCaSecret is required",
        ),
        (
            (*a, "--set", "secrets.receipts.callbackSecret.existingSecret="),
            "callbackSecret.existingSecret is required",
        ),
        ((*a, "--set", "receipts.path=update-status"), "receipts.path must start with /"),
        (("--set", "image.digest=latest"), "image.digest must look like"),
        (("--set", "secrets.core.existingSecret="), "secrets.core.existingSecret is required"),
        (("--set", "database.existingSecret="), "database.existingSecret is required"),
        ((*a, "--set", "cleanup.mode=sidecar"), "cleanup.mode must be"),
    ]
    for extra, needle in cases:
        check(needle in error(*extra), f"guard: {needle} ({' '.join(extra[-1:])})")
    check(not error("--set", "config.policy.dedupWindow=86400"), "integer policy via --set renders")
    check(
        not error(*a, "--set", "receipts.service.type=NodePort"),
        "NodePort receipts Service renders",
    )
    check(
        not error(
            *a,
            "--set", "secrets.receipts.pinnedCertKeys=null",
            "--set", "secrets.receipts.clientCaSecret=oan-messaging-8028-callback-ca",
            "--set", "secrets.receipts.clientCaKeys={ca.crt}",
        ),
        "receipts with 8028's callback CA and no pins render",
    )


def main() -> int:
    for step in (names, budget, migration, guards):
        step()
    print(f"\n{passed} checks passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

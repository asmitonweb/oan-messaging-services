#!/usr/bin/env bash
# Repeatable static validation for the oan-messaging chart (MSG-08H). Contacts no cluster.
#
#   deploy/helm/oan-messaging/ci/run-regressions.sh
#
# Requires: helm (v3.14+ or v4), python3 with PyYAML, openssl. Optional: kubeconform.
# Env: PYTHON (default python3), KUBE_VERSIONS (default "1.29.0 1.31.0 1.33.0").
set -euo pipefail

CHART="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
KUBE_VERSIONS="${KUBE_VERSIONS:-1.29.0 1.31.0 1.33.0}"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/oan-helm-ci.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT

echo "== tools"
helm version --short
"$PYTHON" -c 'import sys, yaml; print("python", sys.version.split()[0], "pyyaml", yaml.__version__)'
openssl version

echo "== helm lint"
helm lint "$CHART"
for values in "$CHART"/ci/*-values.yaml; do
  helm lint "$CHART" -f "$values"
done

echo "== helm template"
for values in "$CHART"/ci/*-values.yaml; do
  name="$(basename "$values" .yaml)"
  helm template oan-msg "$CHART" -n oan-messaging-dev -f "$values" > "$OUT/$name.yaml"
  printf '%-45s %s resources\n' "$name" "$(grep -c '^kind:' "$OUT/$name.yaml")"
done

echo "== secret-material scan (rendered output and chart sources)"
if grep -rnE 'BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)|oanmsg_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|[0-9]{8,10}:[A-Za-z0-9_-]{35}' \
     "$OUT" "$CHART/values.yaml" "$CHART/templates" "$CHART/ci"/*-values.yaml; then
  echo "FAIL: secret-like material found"; exit 1
fi
if grep -q '^kind: Secret$' "$OUT"/*.yaml; then echo "FAIL: chart renders a Secret"; exit 1; fi
echo "none found; no Secret objects rendered"

echo "== kubeconform"
if command -v kubeconform >/dev/null 2>&1; then
  for v in $KUBE_VERSIONS; do
    printf 'Kubernetes %s: ' "$v"
    kubeconform -strict -summary -kubernetes-version "$v" "$OUT"/*.yaml
  done
else
  echo "SKIPPED: kubeconform not installed (schema validation not run)"
fi

echo "== snapshot script regression (files/tls_snapshot.py)"
"$PYTHON" -m unittest discover -s "$CHART/ci/regression" -p 'test_*.py'

echo "== render regression"
"$PYTHON" "$CHART/ci/regression/check_render.py"

echo "== all regression checks passed"

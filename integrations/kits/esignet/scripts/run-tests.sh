#!/usr/bin/env bash
# Compile OanOtpSender for Java 17 and run its tests against a local HTTPS stand-in for
# OAN. Needs a JDK 17 or later (javac, java, keytool); nothing is downloaded.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "${work:?}"' EXIT
pass="test-only-$RANDOM$RANDOM"
keytool -genkeypair -alias oan-test -keyalg RSA -keysize 2048 -validity 2 \
  -dname "CN=localhost" -ext "SAN=dns:localhost,ip:127.0.0.1" \
  -storetype PKCS12 -keystore "$work/test.p12" -storepass "$pass" -keypass "$pass" >/dev/null 2>&1
javac --release 17 -Xlint:all -Werror -d "$work/classes" \
  "$here/src/main/java/org/openagrinet/messaging/OanOtpSender.java" \
  "$here/src/test/java/org/openagrinet/messaging/OanOtpSenderTest.java"
java -cp "$work/classes" org.openagrinet.messaging.OanOtpSenderTest "$work/test.p12" "$pass"

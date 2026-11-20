# eSignet kit: login OTPs through OAN Messaging (Java)

| | |
|---|---|
| **For** | The eSignet team (OAN's eSignet deployment and its OTP plugin are theirs) |
| **What** | `OanOtpSender`, one Java class with no dependencies beyond the JDK (Java 17 or later), and its tests |
| **Handoff guide** | 15 |

No eSignet source was available, so this is not a patch: it is the sender eSignet calls at
its existing OTP delivery point, after the OTP state is stored, in place of the old SMS
gateway (guide 15 §3 step 1).

## Use it

```java
OanOtpSender sender = new OanOtpSender(OanOtpSender.Config.of(
        URI.create("https://oan-messaging.<env>.example.invalid"), tokenFromSecretStore));

OanOtpSender.OtpSms sms = new OanOtpSender.OtpSms(
        OanOtpSender.deliveryKey("esignet", transactionId, "otp-" + otpSequence, "sms"),
        linkedPhone,                          // 09…, 07…, 2519… or +2519…
        renderedOtpText,                      // one SMS part
        "esignet:otp:" + transactionId,
        otpExpiresAt);                        // the code's own expiry, at most 30 minutes ahead

switch (sender.send(sms)) {
    case OanOtpSender.Accepted a -> { /* log transactionId and a.messageId() only */ }
    case OanOtpSender.Rejected r -> { /* definite refusal (r.status()): alert; do not retry */ }
    case OanOtpSender.Uncertain u -> { /* OAN may hold it: retry later with the same sms, or let the user ask again */ }
}
```

* **Keys:** `deliveryKey` gives the same UUID as the OAN Python client's `delivery_key`
  (checked against it in the tests). A user's resend is a new code: increment the
  sequence, so it gets a new key. Never put the phone or the code into key inputs; the
  method refuses phone-like values.
* **Retries:** uncertain outcomes (timeout, connection error, 429, 5xx) are retried with
  the same key and body, never sooner than `Retry-After`; a `Retry-After` beyond
  `maxRetryAfter` returns `Uncertain` at once. Any other 4xx is `Rejected` and never
  retried. A replay (200) returns the original message id.
* **Validation before sending:** an Ethiopian mobile, text that fits one SMS part, a
  future expiry at most 30 minutes ahead, valid key and reference. Otherwise
  `IllegalArgumentException`, and nothing is sent.
* **Privacy:** results, exceptions and `toString` never contain the phone, the text or the
  token. Keep them out of eSignet's own logs too (guide 15 §3 step 5).
* **TLS:** HTTPS only; redirects are not followed. For a private CA, pass an `HttpClient`
  built with that CA in its `SSLContext` to the three-argument constructor.

## Test

```
scripts/run-tests.sh
```

Compiles for Java 17 with all warnings as errors, generates a throwaway local
certificate, and runs the 13 tests against a local HTTPS stand-in for OAN.

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| `javac --release 17 -Xlint:all -Werror` | Clean |
| Tests (JDK 25) | 13 passed: keys equal to the Python client's; one-SMS rule; accepted, replay, refusal not retried, same-key retries, give-up, `Retry-After` budget, validation, unreachable OAN, nothing sensitive printable, HTTPS only |
| End to end against the OAN Messaging Service over HTTPS (trial, caller `esignet`) and the fake 8028 | Accepted; the same SMS sent again came back as a replay with the same message id; one SMS at the gateway |

## Still for the eSignet team

Wire it into the OTP delivery point and remove the old gateway call; confirm the field
names marked *assumed* in guide 15; check at configuration time that the OTP lifetime is
within 30 minutes and the templates fit one SMS; QA cases ES-01 to ES-13.

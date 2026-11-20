package org.openagrinet.messaging;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Sends one-time codes as SMS through the OAN Messaging Service (contract 3.0.0).
 *
 * <p>For eSignet's OTP delivery point: call {@link #send(OtpSms)} after the OTP state is
 * stored, instead of the old SMS gateway. The rules it applies (handoff guide 15):
 *
 * <ul>
 *   <li>purpose {@code otp}; {@code expires_at} is the code's own expiry, at most 30 minutes
 *       ahead;
 *   <li>one Idempotency-Key per issued code ({@link #deliveryKey}); an uncertain outcome
 *       (timeout, connection error, 429 or 5xx) is retried with the same key and body, and
 *       never sooner than the server's {@code Retry-After};
 *   <li>a definite refusal (any other 4xx) is never retried;
 *   <li>the text fits one SMS part (160 GSM-7 characters, otherwise 70 UTF-16 units);
 *   <li>no result, exception or {@code toString} contains the phone, the text or the token.
 * </ul>
 *
 * <p>A user's resend is a new code: give it a new sequence number, and so a new key.
 * Thread-safe; build one instance per configuration and reuse it.
 */
public final class OanOtpSender {

    /** Fixed namespace of OAN delivery keys (the Python client's {@code OAN_KEY_NAMESPACE}). */
    public static final UUID KEY_NAMESPACE = UUID.fromString("5b0f6c1e-6f2a-4d4e-9c63-2b7a1f0d8e41");
    /** Version tag hashed into every key ({@code KEY_ALGORITHM}). */
    public static final String KEY_ALGORITHM = "oan-delivery-key/v2";
    /** OAN refuses an OTP that may live longer than this. */
    public static final Duration MAX_OTP_LIFETIME = Duration.ofMinutes(30);

    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9._:-]{1,64}");
    private static final Pattern PHONE_LIKE = Pattern.compile("(?<!\\d)(?:\\+?251|0)[79]\\d{8}(?!\\d)");
    private static final Pattern ET_MOBILE = Pattern.compile("\\+?251[79][0-9]{8}|0[79][0-9]{8}");
    private static final Pattern KEY = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:\\-]{7,127}");
    private static final Pattern SOURCE_REFERENCE = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}");
    private static final Pattern MESSAGE_ID = Pattern.compile("\"id\"\\s*:\\s*\"([0-9a-fA-F-]{36})\"");
    private static final Set<Integer> RETRYABLE = Set.of(429, 500, 502, 503, 504);
    private static final String GSM7_BASIC = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            + "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
    private static final String GSM7_EXTENSION = "^{}\\[~]|€\f";

    /** Where and how to reach OAN. Load {@code token} from eSignet's secret store. */
    public record Config(URI baseUrl, String token, Duration connectTimeout, Duration readTimeout,
                         int maxAttempts, Duration backoff, Duration maxRetryAfter) {
        public Config {
            Objects.requireNonNull(baseUrl, "baseUrl");
            if (!"https".equals(baseUrl.getScheme()) || baseUrl.getHost() == null) {
                throw new IllegalArgumentException("baseUrl must be an https URL");
            }
            if (token == null || token.isBlank()) {
                throw new IllegalArgumentException("token is required");
            }
            if (maxAttempts < 1 || maxAttempts > 10) {
                throw new IllegalArgumentException("maxAttempts must be between 1 and 10");
            }
        }

        /** Defaults: 5 s connect, 10 s read, 3 attempts, 0.5 s backoff, wait out at most 30 s. */
        public static Config of(URI baseUrl, String token) {
            return new Config(baseUrl, token, Duration.ofSeconds(5), Duration.ofSeconds(10), 3,
                    Duration.ofMillis(500), Duration.ofSeconds(30));
        }

        @Override
        public String toString() {
            return "Config[baseUrl=" + baseUrl + ", token=<redacted>]";
        }
    }

    /** One OTP SMS. Keep it (key, text, expiry) until the code expires, for same-key retries. */
    public record OtpSms(String idempotencyKey, String recipient, String text, String sourceReference,
                         Instant expiresAt) {
        @Override
        public String toString() {
            String key = idempotencyKey == null ? "" : idempotencyKey;
            return "OtpSms[key=…" + key.substring(Math.max(0, key.length() - 4))
                    + ", sourceReference=" + sourceReference + "]";
        }
    }

    /** What happened. Never carries the phone, the text or the token. */
    public sealed interface Result permits Accepted, Rejected, Uncertain {
    }

    /** OAN stored the message (202), or it already had it under this key (200 replay). */
    public record Accepted(String messageId, boolean replayed) implements Result {
    }

    /** A definite refusal: fix the request or configuration; never retry it as is. */
    public record Rejected(int status) implements Result {
    }

    /** No clear answer after every attempt: OAN may hold it. Retry later with the same SMS only. */
    public record Uncertain(String lastOutcome) implements Result {
    }

    private final Config config;
    private final HttpClient http;
    private final Clock clock;

    public OanOtpSender(Config config) {
        this(config, HttpClient.newBuilder().connectTimeout(config.connectTimeout())
                .followRedirects(HttpClient.Redirect.NEVER).build(), Clock.systemUTC());
    }

    /** For tests and custom TLS (a private CA through the client's {@code SSLContext}). */
    public OanOtpSender(Config config, HttpClient http, Clock clock) {
        this.config = Objects.requireNonNull(config, "config");
        this.http = Objects.requireNonNull(http, "http");
        this.clock = Objects.requireNonNull(clock, "clock");
    }

    /**
     * The key for one issued code: a UUID version 5 over
     * {@code ["oan-delivery-key/v2", service, eventId, role, channel]}, identical to the
     * OAN Python client's {@code delivery_key}. Example:
     * {@code deliveryKey("esignet", transactionId, "otp-" + otpSequence, "sms")}.
     * Inputs are short opaque tokens and must never contain a phone number or the code.
     */
    public static String deliveryKey(String service, String eventId, String role, String channel) {
        String[] parts = {service, eventId, role, channel};
        StringBuilder material = new StringBuilder("[\"").append(KEY_ALGORITHM).append('"');
        for (String part : parts) {
            if (part == null || !TOKEN.matcher(part).matches()) {
                throw new IllegalArgumentException("key inputs must be short opaque tokens");
            }
            if (PHONE_LIKE.matcher(part).find()) {
                throw new IllegalArgumentException("key inputs must not contain phone numbers");
            }
            material.append(",\"").append(part).append('"');
        }
        material.append(']');
        return uuid5(KEY_NAMESPACE, material.toString()).toString();
    }

    /** Whether {@code text} fits one SMS part, by the rule OAN enforces. */
    public static boolean fitsOneSms(String text) {
        int septets = 0;
        boolean gsm = true;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (GSM7_BASIC.indexOf(c) >= 0) {
                septets += 1;
            } else if (GSM7_EXTENSION.indexOf(c) >= 0) {
                septets += 2;
            } else {
                gsm = false;
                break;
            }
        }
        return gsm ? septets <= 160 : text.length() <= 70;
    }

    /**
     * Submit one OTP SMS, retrying uncertain outcomes with the same key and body.
     *
     * @throws IllegalArgumentException when the SMS itself is invalid (nothing is sent)
     * @throws InterruptedException when interrupted while waiting to retry
     */
    public Result send(OtpSms sms) throws InterruptedException {
        HttpRequest request = request(sms, validateAndRender(sms));
        String last = "none";
        for (int attempt = 1; attempt <= config.maxAttempts(); attempt++) {
            Duration wait = config.backoff().multipliedBy(1L << (attempt - 1));
            HttpResponse<String> response;
            try {
                response = http.send(request, HttpResponse.BodyHandlers.ofString());
            } catch (IOException e) {
                response = null;
                last = e.getClass().getSimpleName();
            }
            if (response != null) {
                Result definite = definiteResult(response);
                if (definite != null) {
                    return definite;
                }
                last = "http_" + response.statusCode();
                Duration retryAfter = retryAfter(response);
                if (retryAfter != null && retryAfter.compareTo(config.maxRetryAfter()) > 0) {
                    return new Uncertain(last + "_retry_after_too_long");
                }
                wait = longer(wait, retryAfter);
            }
            if (attempt < config.maxAttempts()) {
                Thread.sleep(wait.toMillis());
            }
        }
        return new Uncertain(last);
    }

    private HttpRequest request(OtpSms sms, String body) {
        return HttpRequest.newBuilder(config.baseUrl().resolve("/api/v1/messages/sms"))
                .timeout(config.readTimeout())
                .header("Authorization", "Bearer " + config.token())
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .header("Idempotency-Key", sms.idempotencyKey())
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
    }

    /** Accepted or Rejected; null when the outcome is uncertain and worth retrying. */
    private static Result definiteResult(HttpResponse<String> response) {
        int status = response.statusCode();
        if (status == 202 || status == 200) {
            Matcher id = MESSAGE_ID.matcher(response.body());
            return id.find() ? new Accepted(id.group(1), status == 200) : new Uncertain("unexpected_response");
        }
        return RETRYABLE.contains(status) ? null : new Rejected(status);
    }

    private static Duration longer(Duration a, Duration b) {
        return b != null && b.compareTo(a) > 0 ? b : a;
    }

    private String validateAndRender(OtpSms sms) {
        Objects.requireNonNull(sms, "sms");
        if (sms.idempotencyKey() == null || !KEY.matcher(sms.idempotencyKey()).matches()) {
            throw new IllegalArgumentException("idempotencyKey must be 8-128 chars of [A-Za-z0-9._:-]");
        }
        if (sms.recipient() == null || !ET_MOBILE.matcher(sms.recipient()).matches()) {
            throw new IllegalArgumentException("recipient must be an Ethiopian mobile number (09/07 series)");
        }
        if (sms.text() == null || sms.text().isEmpty() || !fitsOneSms(sms.text())) {
            throw new IllegalArgumentException("text is empty or does not fit one SMS part");
        }
        if (sms.sourceReference() == null || !SOURCE_REFERENCE.matcher(sms.sourceReference()).matches()) {
            throw new IllegalArgumentException("sourceReference has an invalid format");
        }
        Instant now = clock.instant();
        if (sms.expiresAt() == null || !sms.expiresAt().isAfter(now)) {
            throw new IllegalArgumentException("expiresAt must be in the future");
        }
        if (sms.expiresAt().isAfter(now.plus(MAX_OTP_LIFETIME))) {
            throw new IllegalArgumentException("expiresAt must be at most 30 minutes ahead for an OTP");
        }
        return "{\"recipient\":" + json(sms.recipient()) + ",\"text\":" + json(sms.text())
                + ",\"source_reference\":" + json(sms.sourceReference()) + ",\"purpose\":\"otp\""
                + ",\"expires_at\":" + json(DateTimeFormatter.ISO_INSTANT.format(sms.expiresAt())) + "}";
    }

    private static Duration retryAfter(HttpResponse<String> response) {
        return response.headers().firstValue("Retry-After")
                .filter(v -> v.matches("[0-9]{1,6}"))
                .map(v -> Duration.ofSeconds(Long.parseLong(v)))
                .orElse(null);
    }

    private static String json(String value) {
        StringBuilder out = new StringBuilder("\"");
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        return out.append('"').toString();
    }

    private static UUID uuid5(UUID namespace, String name) {
        try {
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            sha1.update(toBytes(namespace));
            byte[] hash = sha1.digest(name.getBytes(StandardCharsets.UTF_8));
            hash[6] = (byte) ((hash[6] & 0x0f) | 0x50);
            hash[8] = (byte) ((hash[8] & 0x3f) | 0x80);
            long msb = 0;
            long lsb = 0;
            for (int i = 0; i < 8; i++) {
                msb = (msb << 8) | (hash[i] & 0xff);
                lsb = (lsb << 8) | (hash[i + 8] & 0xff);
            }
            return new UUID(msb, lsb);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-1 is not available", e);
        }
    }

    private static byte[] toBytes(UUID uuid) {
        byte[] out = new byte[16];
        long msb = uuid.getMostSignificantBits();
        long lsb = uuid.getLeastSignificantBits();
        for (int i = 0; i < 8; i++) {
            out[i] = (byte) (msb >>> (8 * (7 - i)));
            out[i + 8] = (byte) (lsb >>> (8 * (7 - i)));
        }
        return out;
    }
}

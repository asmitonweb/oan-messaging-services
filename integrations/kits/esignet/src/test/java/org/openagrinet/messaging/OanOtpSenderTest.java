package org.openagrinet.messaging;

import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

/**
 * Tests for {@link OanOtpSender} with no dependencies beyond the JDK: a local HTTPS server
 * plays the OAN Messaging API. Run with {@code scripts/run-tests.sh}. Test numbers only.
 */
public final class OanOtpSenderTest {

    private static final String PHONE = "0900000001";
    private static final String TEXT = "Your OAN code is 482913. It expires in 5 minutes.";
    private static final String TOKEN = "oanmsg_test-token-not-a-secret";
    private static final Instant NOW = Instant.parse("2026-09-01T05:12:13Z");
    private static final Clock CLOCK = Clock.fixed(NOW, ZoneOffset.UTC);

    /** A scripted OAN API: each request takes the next status; records what it received. */
    static final class FakeOan {
        final LinkedList<Integer> statuses = new LinkedList<>();
        final List<Map<String, String>> requests = Collections.synchronizedList(new ArrayList<>());
        String retryAfter;
    }

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        String keystore = args[0];
        char[] password = args[1].toCharArray();
        SSLContext ssl = sslContext(keystore, password);
        FakeOan fake = new FakeOan();
        HttpsServer server = HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setHttpsConfigurator(new HttpsConfigurator(ssl));
        server.createContext("/api/v1/messages/sms", exchange -> {
            byte[] body = exchange.getRequestBody().readAllBytes();
            fake.requests.add(Map.of(
                    "key", String.valueOf(exchange.getRequestHeaders().getFirst("Idempotency-Key")),
                    "auth", String.valueOf(exchange.getRequestHeaders().getFirst("Authorization")),
                    "body", new String(body, StandardCharsets.UTF_8)));
            Integer status = fake.statuses.isEmpty() ? 202 : fake.statuses.removeFirst();
            byte[] out = (status == 202 || status == 200)
                    ? "{\"id\":\"0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10\",\"status\":\"accepted\"}".getBytes()
                    : "{\"title\":\"x\",\"code\":\"x\",\"errors\":[]}".getBytes();
            if (fake.retryAfter != null && status != 202 && status != 200) {
                exchange.getResponseHeaders().add("Retry-After", fake.retryAfter);
            }
            exchange.sendResponseHeaders(status, out.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(out);
            }
        });
        server.start();
        URI base = URI.create("https://127.0.0.1:" + server.getAddress().getPort());
        HttpClient http = HttpClient.newBuilder().sslContext(ssl).connectTimeout(Duration.ofSeconds(2)).build();
        OanOtpSender.Config config = new OanOtpSender.Config(base, TOKEN, Duration.ofSeconds(2),
                Duration.ofSeconds(2), 3, Duration.ofMillis(10), Duration.ofSeconds(1));
        OanOtpSender sender = new OanOtpSender(config, http, CLOCK);
        Supplier<OanOtpSender.OtpSms> sms = () -> new OanOtpSender.OtpSms(
                OanOtpSender.deliveryKey("esignet", "tx-7f3c9a", "otp-1", "sms"), PHONE, TEXT,
                "esignet:otp:tx-7f3c9a", NOW.plus(Duration.ofMinutes(5)));

        test("delivery keys match the OAN Python client", () -> {
            check("c825d2f1-d07c-5205-a0e4-1dd730499024".equals(OanOtpSender.deliveryKey("esignet", "tx-7f3c9a", "otp-1", "sms")), "esignet otp-1");
            check("8e51fb9e-71b2-5a17-bb37-c6dc0d9a8787".equals(OanOtpSender.deliveryKey("esignet", "tx-7f3c9a", "otp-2", "sms")), "esignet otp-2");
            check("893b513b-376d-5fa9-9cee-e99a94114d5b".equals(OanOtpSender.deliveryKey("a2c", "APP-2026-00001", "farmer", "sms")), "a2c");
        });
        test("key inputs refuse phone numbers and free text", () -> {
            refuses(() -> OanOtpSender.deliveryKey("esignet", "tx-0911000000", "otp-1", "sms"));
            refuses(() -> OanOtpSender.deliveryKey("esignet", "tx 1", "otp-1", "sms"));
        });
        test("one SMS part: 160 GSM-7, extension characters count 2, else 70", () -> {
            check(OanOtpSender.fitsOneSms("a".repeat(160)), "160 GSM-7");
            check(!OanOtpSender.fitsOneSms("a".repeat(161)), "161 GSM-7");
            check(!OanOtpSender.fitsOneSms("€".repeat(81)), "81 extension characters");
            check(OanOtpSender.fitsOneSms("ሀ".repeat(70)), "70 Amharic");
            check(!OanOtpSender.fitsOneSms("ሀ".repeat(71)), "71 Amharic");
        });
        test("accepted: one request with the OTP body, key and token", () -> {
            reset(fake);
            OanOtpSender.Result result = sender.send(sms.get());
            check(result instanceof OanOtpSender.Accepted a && !a.replayed()
                    && a.messageId().equals("0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10"), "accepted " + result);
            check(fake.requests.size() == 1, "one request");
            Map<String, String> r = fake.requests.get(0);
            check(r.get("key").equals("c825d2f1-d07c-5205-a0e4-1dd730499024"), "key header");
            check(r.get("auth").equals("Bearer " + TOKEN), "bearer token");
            check(r.get("body").equals("{\"recipient\":\"0900000001\",\"text\":\"" + TEXT
                    + "\",\"source_reference\":\"esignet:otp:tx-7f3c9a\",\"purpose\":\"otp\""
                    + ",\"expires_at\":\"2026-09-01T05:17:13Z\"}"), "body " + r.get("body"));
        });
        test("a replay (200) is accepted and marked as a replay", () -> {
            reset(fake);
            fake.statuses.add(200);
            check(sender.send(sms.get()) instanceof OanOtpSender.Accepted a && a.replayed(), "replayed");
        });
        test("a refusal is never retried", () -> {
            reset(fake);
            fake.statuses.add(422);
            check(sender.send(sms.get()) instanceof OanOtpSender.Rejected r && r.status() == 422, "422");
            check(fake.requests.size() == 1, "one request");
        });
        test("uncertain outcomes are retried with the same key and body", () -> {
            reset(fake);
            fake.statuses.addAll(List.of(503, 429, 202));
            check(sender.send(sms.get()) instanceof OanOtpSender.Accepted, "accepted on the third");
            check(fake.requests.size() == 3, "three requests");
            check(fake.requests.stream().map(m -> m.get("key") + m.get("body")).distinct().count() == 1,
                    "same key and body");
        });
        test("still uncertain after every attempt", () -> {
            reset(fake);
            fake.statuses.addAll(List.of(503, 503, 503));
            check(sender.send(sms.get()) instanceof OanOtpSender.Uncertain u && u.lastOutcome().equals("http_503"),
                    "uncertain http_503");
        });
        test("a Retry-After beyond the wait budget stops without retrying early", () -> {
            reset(fake);
            fake.statuses.addAll(List.of(503, 202));
            fake.retryAfter = "60";
            check(sender.send(sms.get()) instanceof OanOtpSender.Uncertain, "uncertain");
            check(fake.requests.size() == 1, "no early retry");
        });
        test("invalid messages are refused before anything is sent", () -> {
            reset(fake);
            OanOtpSender.OtpSms ok = sms.get();
            refuses(() -> sender.send(new OanOtpSender.OtpSms(ok.idempotencyKey(), PHONE, TEXT, ok.sourceReference(),
                    NOW.plus(Duration.ofMinutes(31)))));
            refuses(() -> sender.send(new OanOtpSender.OtpSms(ok.idempotencyKey(), PHONE, TEXT, ok.sourceReference(), NOW)));
            refuses(() -> sender.send(new OanOtpSender.OtpSms(ok.idempotencyKey(), "+254712345678", TEXT,
                    ok.sourceReference(), ok.expiresAt())));
            refuses(() -> sender.send(new OanOtpSender.OtpSms(ok.idempotencyKey(), PHONE, "x".repeat(161),
                    ok.sourceReference(), ok.expiresAt())));
            refuses(() -> sender.send(new OanOtpSender.OtpSms("short", PHONE, TEXT, ok.sourceReference(), ok.expiresAt())));
            check(fake.requests.isEmpty(), "nothing sent");
        });
        test("an unreachable OAN is uncertain, not an exception", () -> {
            OanOtpSender.Config closed = new OanOtpSender.Config(URI.create("https://127.0.0.1:1"), TOKEN,
                    Duration.ofMillis(300), Duration.ofMillis(300), 2, Duration.ofMillis(10), Duration.ofSeconds(1));
            check(new OanOtpSender(closed, http, CLOCK).send(sms.get()) instanceof OanOtpSender.Uncertain, "uncertain");
        });
        test("nothing printable holds the phone, the text or the token", () -> {
            String printed = sms.get() + " " + config + " " + new OanOtpSender.Rejected(422);
            check(!printed.contains(PHONE) && !printed.contains("482913") && !printed.contains(TOKEN), printed);
        });
        test("http is refused", () -> refuses(() -> OanOtpSender.Config.of(URI.create("http://oan.example.invalid"), TOKEN)));

        server.stop(0);
        System.out.println(passed + " passed, " + failures.size() + " failed");
        failures.forEach(f -> System.out.println("FAILED: " + f));
        System.exit(failures.isEmpty() ? 0 : 1);
    }

    interface Body {
        void run() throws Exception;
    }

    private static void test(String name, Body body) {
        int before = failures.size();
        try {
            body.run();
        } catch (Exception | AssertionError e) {
            failures.add(name + ": " + e);
        }
        if (failures.size() == before) {
            passed++;
            System.out.println("ok   " + name);
        } else {
            System.out.println("FAIL " + name);
        }
    }

    private static void check(boolean condition, String what) {
        if (!condition) {
            throw new AssertionError(what);
        }
    }

    private static void refuses(Body body) throws Exception {
        try {
            body.run();
        } catch (IllegalArgumentException expected) {
            return;
        }
        throw new AssertionError("expected IllegalArgumentException");
    }

    private static void reset(FakeOan fake) {
        fake.statuses.clear();
        fake.requests.clear();
        fake.retryAfter = null;
    }

    private static SSLContext sslContext(String path, char[] password) throws Exception {
        KeyStore store = KeyStore.getInstance("PKCS12");
        try (FileInputStream in = new FileInputStream(path)) {
            store.load(in, password);
        }
        KeyManagerFactory keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        keys.init(store, password);
        TrustManagerFactory trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        trust.init(store);
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(keys.getKeyManagers(), trust.getTrustManagers(), null);
        return context;
    }

    private OanOtpSenderTest() {
    }
}

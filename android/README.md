# Actual Clerk companion (Android)

A single-purpose app: it forwards a card app's notifications to Actual Clerk
so a charge counts against the budget the moment it is made. Clerk turns each
into an anticipated charge and settles it when the bank's own transaction
arrives; nothing is written into Actual Budget.

Build it without an Android toolchain on the host:

```bash
scripts/build-android.sh            # from the repository root
# → dist/android/actual-clerk-companion.apk
```

How it works, how to pair it, and the endpoints it uses are described in
[docs/anticipated-charges.md](../docs/anticipated-charges.md).

Layout:

| Path | What |
| --- | --- |
| `app/src/main/java/.../ClerkNotificationListener.kt` | The notification listener; hands registered apps' notifications to a worker |
| `app/src/main/java/.../ForwardWorker.kt` | WorkManager job that delivers one notification to Clerk with retries |
| `app/src/main/java/.../ClerkApi.kt` | The five calls the phone makes, over `HttpURLConnection` |
| `app/src/main/java/.../Prefs.kt` | Server, token, device id, registered sources, a short local log |
| `app/src/main/java/.../MainActivity.kt` | Compose UI: connect, home, register a source, recent charges |
| `Dockerfile` | Android SDK + Gradle build, exporting the signed APK as a build artifact |
| `keystore/` | The signing key the build script creates on first use (ignored by git) |

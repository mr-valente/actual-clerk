# syntax=docker/dockerfile:1.7
#
# Builds the Actual Clerk phone app without an Android toolchain on the host.
#
#   docker build -f android/Dockerfile --target apk --output type=local,dest=dist/android android
#
# scripts/build-android.sh wraps this and keeps the signing key stable between
# builds. Without a key in android/keystore/ a throwaway one is generated so
# the build still produces an installable APK, but an update signed with a
# different key cannot be installed over the previous one.

FROM eclipse-temurin:17-jdk-jammy AS build

ARG CMDLINE_TOOLS_VERSION=11076708
ARG GRADLE_VERSION=8.9
ARG ANDROID_PLATFORM=android-35
ARG BUILD_TOOLS=35.0.0

ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    GRADLE_HOME=/opt/gradle \
    GRADLE_USER_HOME=/root/.gradle
ENV PATH=${GRADLE_HOME}/bin:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p ${ANDROID_HOME}/cmdline-tools \
    && curl -fsSL -o /tmp/cmdline-tools.zip \
        https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip \
    && unzip -q /tmp/cmdline-tools.zip -d ${ANDROID_HOME}/cmdline-tools \
    && mv ${ANDROID_HOME}/cmdline-tools/cmdline-tools ${ANDROID_HOME}/cmdline-tools/latest \
    && rm /tmp/cmdline-tools.zip \
    && yes | sdkmanager --licenses > /dev/null \
    && sdkmanager "platform-tools" "platforms;${ANDROID_PLATFORM}" "build-tools;${BUILD_TOOLS}" > /dev/null

RUN curl -fsSL -o /tmp/gradle.zip https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && mv /opt/gradle-${GRADLE_VERSION} ${GRADLE_HOME} \
    && rm /tmp/gradle.zip

WORKDIR /src
COPY settings.gradle.kts build.gradle.kts gradle.properties ./
COPY keystore ./keystore
COPY app ./app

ARG KEYSTORE_PASSWORD=actual-clerk
ARG APP_VERSION=0.1.0
ARG APP_VERSION_CODE=1
ENV CLERK_KEYSTORE=/src/keystore/clerk.jks \
    CLERK_KEYSTORE_PASSWORD=${KEYSTORE_PASSWORD} \
    CLERK_APP_VERSION=${APP_VERSION} \
    CLERK_APP_VERSION_CODE=${APP_VERSION_CODE}

RUN if [ ! -f keystore/clerk.jks ]; then \
        echo "WARNING: no android/keystore/clerk.jks in the build context; generating a throwaway key." >&2; \
        keytool -genkeypair -keystore keystore/clerk.jks -alias clerk -keyalg RSA -keysize 2048 \
            -validity 10000 -storepass "${KEYSTORE_PASSWORD}" -keypass "${KEYSTORE_PASSWORD}" \
            -dname "CN=Actual Clerk companion, O=Actual Clerk" > /dev/null; \
    fi

RUN --mount=type=cache,target=/root/.gradle \
    gradle --no-daemon --console=plain assembleRelease \
    && mkdir -p /out \
    && cp app/build/outputs/apk/release/app-release.apk /out/actual-clerk-companion.apk \
    && ls -l /out

FROM scratch AS apk
COPY --from=build /out/actual-clerk-companion.apk /actual-clerk-companion.apk

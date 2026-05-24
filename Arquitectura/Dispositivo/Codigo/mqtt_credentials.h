#ifndef MQTT_CREDENTIALS_H_
#define MQTT_CREDENTIALS_H_

static const char MQTT_CLIENT_CERT_PEM[] = R"PEM(-----BEGIN CERTIFICATE-----
<Certificado generado por AWS IoT Core>
-----END CERTIFICATE-----)PEM";

static const char MQTT_CLIENT_KEY_PEM[] = R"PEM(-----BEGIN RSA PRIVATE KEY-----
<Clave privada generada por AWS IoT Core>
-----END RSA PRIVATE KEY-----)PEM";

#endif

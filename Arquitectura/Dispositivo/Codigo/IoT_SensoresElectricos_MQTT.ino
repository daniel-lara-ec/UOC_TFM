#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <PubSubClient.h>
#include <SSLClient.h>
#include <SSLClientParameters.h>
#include <PZEM004Tv30.h>
#include "mqtt_credentials.h"
#include <time.h>
#include"certs.h"



// https://openslab-osu.github.io/bearssl-certificate-utility/
// ================= CONFIG =================
#define ETH_CS 5
#define RNG_PIN 34
#define LED_STATUS 4

#define AWS_IOT_ENDPOINT "<endpoint generado por AWS IoT Core>"

#define AWS_IOT_PORT 8883

#ifndef MQTT_CLIENT_ID_PREFIX
#define MQTT_CLIENT_ID_PREFIX "dispositivo_001"
#endif

#define ID_CLIENTE "cliente_001"

#define ID_DISPOSITIVO "dispositivo_001"

// ================= TIMING CONFIG =================
const unsigned long INTERVALO_SERIAL = 15000;
const unsigned long INTERVALO_ENVIO = 60000;
const unsigned long LED_PULSE_MS = 50;
const unsigned long LOOP_DELAY_MS = 500;
const unsigned long INTERVALO_REINICIO_MS = 270000UL; // 4.5 minutos
const unsigned long INTERVALO_LOG_REINICIO_MS = 60000;
const unsigned long INTERVALO_REINTENTO_NTP = 30000;
const unsigned long INTERVALO_REINTENTO_MQTT = 5000;

// ================= TIMING VARIABLES =================
unsigned long ultimo_serial = 0;
unsigned long ultimo_envio = 0;
unsigned long inicio_ciclo_reinicio = 0;
unsigned long ultimo_log_reinicio = 0;
int ultimo_minuto_enviado = -1;
bool ntp_sincronizado = false;
unsigned long ultimo_intento_ntp = 0;
unsigned long ultimo_intento_mqtt = 0;

// ================= PZEM HARDWARE =================
HardwareSerial PZEMSerial1(2);
PZEM004Tv30 pzem1(PZEMSerial1, 16, 17);

HardwareSerial PZEMSerial2(1);
PZEM004Tv30 pzem2(PZEMSerial2, 25, 26);

// ================= ETHERNET + MQTT CLIENTS =================
byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
EthernetClient ethClient;
EthernetUDP udp;
SSLClient sslClient(ethClient, TAs, TAs_NUM, RNG_PIN, 10240, SSLClient::SSL_WARN);
PubSubClient mqttClient(AWS_IOT_ENDPOINT, AWS_IOT_PORT, sslClient);
SSLClientParameters mqttMutualAuth = SSLClientParameters::fromPEM(
  MQTT_CLIENT_CERT_PEM,
  sizeof(MQTT_CLIENT_CERT_PEM),
  MQTT_CLIENT_KEY_PEM,
  sizeof(MQTT_CLIENT_KEY_PEM)
);

// ================= NTP CONFIG =================
const char* ntpServer = "pool.ntp.org";
const int NTP_PACKET_SIZE = 48;
byte packetBuffer[NTP_PACKET_SIZE];
unsigned long epochTime = 0;
unsigned long millisAtSync = 0;
const long timeZoneOffset = -5 * 3600; // UTC-5

// ================= DATA STRUCTURE =================
struct SensorData {
  float voltage;
  float current;
  float power;
  float energy;
  float frequency;
  float pf;
};

SensorData sensor1, sensor2;

// ================= FORWARD DECLARATIONS =================
unsigned long getCurrentEpochUTC();
unsigned long getCurrentEpochLocal();
void checkReinicioProgramado(unsigned long ahora);
void reiniciarDispositivo();
void sendNTPpacket(const char* address);
void printDateTime(unsigned long epoch);
unsigned long compileTimeEpochUTC();
int monthFromAbbrev(const char* month);
bool isLeapYear(int year);
unsigned long daysBeforeMonth(int year, int month);
bool isReasonableEpoch(unsigned long epoch);

bool isEthernetConnected();
bool checkMQTTTransport();
bool connectMQTT();
void maintainMQTTConnection();
String buildMQTTTopic();
String buildMQTTPayload();
String buildSensorJSON(int idSensor, float medicion);
void publishMQTTMessage();

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(500);

  inicio_ciclo_reinicio = millis();
  ultimo_log_reinicio = inicio_ciclo_reinicio;

  initLED();
  initEthernet();
  initPZEM();
  intentarSincronizarNTP();

  sslClient.setTimeout(20000);
  sslClient.setMutualAuthParams(mqttMutualAuth);

  mqttClient.setKeepAlive(30);
  mqttClient.setSocketTimeout(20);
  mqttClient.setBufferSize(1536);

  Serial.println("Reinicio automatico activo: cada 4.5 minutos");
  Serial.println("Sistema MQTT listo");
}

// ================= LOOP =================
void loop() {
  unsigned long ahora = millis();

  pulseStatusLED();

  if (!ntp_sincronizado && (ahora - ultimo_intento_ntp >= INTERVALO_REINTENTO_NTP)) {
    intentarSincronizarNTP();
    ultimo_intento_ntp = ahora;
  }

  if (ntp_sincronizado) {
    if (mqttClient.connected()) {
      mqttClient.loop();
    } else {
      maintainMQTTConnection();
    }
  }

  if (ahora - ultimo_serial >= INTERVALO_SERIAL) {
    readSensors();
    printSerialReadings();
    ultimo_serial = ahora;
  }

  if (ntp_sincronizado) {
    unsigned long currentTime = getCurrentEpochUTC();
    int segundos = currentTime % 60;
    int minuto_actual = (currentTime / 60) % 60;

    if (minuto_actual != ultimo_minuto_enviado && segundos < 5) {
      readSensors();
      publishMQTTMessage();
      ultimo_minuto_enviado = minuto_actual;
    }
  } else {
    if (ahora - ultimo_envio >= INTERVALO_ENVIO) {
      readSensors();
      Serial.println("MQTT en espera: hora no sincronizada");
      ultimo_envio = ahora;
    }
  }

  checkReinicioProgramado(ahora);
  delay(LOOP_DELAY_MS);
}

// ================= INITIALIZATION FUNCTIONS =================
void initLED() {
  pinMode(LED_STATUS, OUTPUT);
  digitalWrite(LED_STATUS, LOW);
}

void initEthernet() {
  Serial.println("Inicializando Ethernet...");
  Ethernet.init(ETH_CS);

  if (Ethernet.begin(mac) == 0) {
    Serial.println("DHCP fallo");
    return;
  }

  delay(2000);

  Serial.print("IP: ");
  Serial.println(Ethernet.localIP());
  Serial.print("Gateway: ");
  Serial.println(Ethernet.gatewayIP());
  Serial.print("DNS: ");
  Serial.println(Ethernet.dnsServerIP());

  if (Ethernet.linkStatus() == LinkOFF) {
    Serial.println("Ethernet no conectado");
    return;
  }

  Serial.println("Ethernet conectado");
}

void initPZEM() {
  Serial.println("Inicializando PZEMs...");
  delay(500);

  PZEMSerial1.begin(9600, SERIAL_8N1, 16, 17);
  PZEMSerial2.begin(9600, SERIAL_8N1, 25, 26);

  Serial.println("PZEMs inicializados");
}

void intentarSincronizarNTP() {
  ntp_sincronizado = false;
  Serial.println("Sincronizando hora con NTP (UDP)...");

  udp.begin(8888);

  for (int intento = 0; intento < 5; intento++) {
    if (intento > 0) {
      Serial.print(".");
    }

    sendNTPpacket(ntpServer);

    unsigned long startTime = millis();
    while (millis() - startTime < 4000) {
      if (udp.parsePacket()) {
        int bytesRead = udp.read(packetBuffer, NTP_PACKET_SIZE);
        if (bytesRead < NTP_PACKET_SIZE) {
          Serial.println("Paquete NTP incompleto");
          continue;
        }

        if ((packetBuffer[0] & 0x07) != 4) {
          Serial.println("Respuesta NTP invalida");
          continue;
        }

        unsigned long highWord = word(packetBuffer[40], packetBuffer[41]);
        unsigned long lowWord = word(packetBuffer[42], packetBuffer[43]);
        unsigned long secsSince1900 = (highWord << 16) | lowWord;

        const unsigned long seventyYears = 2208988800UL;
        Serial.print("NTP raw secsSince1900: ");
        Serial.println(secsSince1900);
        if (secsSince1900 < seventyYears) {
          Serial.println("Hora NTP invalida");
          continue;
        }

        epochTime = secsSince1900 - seventyYears;
        millisAtSync = millis();

        Serial.print("NTP epochTime: ");
        Serial.println(epochTime);
        Serial.print("Compile-time epoch: ");
        Serial.println(compileTimeEpochUTC());

        if (isReasonableEpoch(epochTime)) {
          Serial.println("");
          ntp_sincronizado = true;
          Serial.print("NTP sincronizado: ");
          printDateTime(getCurrentEpochLocal());
          udp.stop();
          return;
        }
      }
      delay(10);
    }
  }

  Serial.println("");
  epochTime = compileTimeEpochUTC();
  millisAtSync = millis();
  ntp_sincronizado = true;
  Serial.print("NTP no disponible - usando hora de compilacion: ");
  printDateTime(getCurrentEpochLocal());
  udp.stop();
}

void sendNTPpacket(const char* address) {
  memset(packetBuffer, 0, NTP_PACKET_SIZE);
  packetBuffer[0] = 0b11100011;
  packetBuffer[1] = 0;
  packetBuffer[2] = 6;
  packetBuffer[3] = 0xEC;
  packetBuffer[12] = 49;
  packetBuffer[13] = 0x4E;
  packetBuffer[14] = 49;
  packetBuffer[15] = 52;

  udp.beginPacket(address, 123);
  udp.write(packetBuffer, NTP_PACKET_SIZE);
  udp.endPacket();
}

void printDateTime(unsigned long epoch) {
  int ss = epoch % 60;
  epoch /= 60;
  int mm = epoch % 60;
  epoch /= 60;
  int hh = epoch % 24;
  epoch /= 24;

  int year = 1970;
  int daysInYear = 365;
  while (epoch >= (unsigned long)daysInYear) {
    epoch -= daysInYear;
    year++;
    daysInYear = (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ? 366 : 365;
  }

  int month = 1;
  int daysInMonth[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) {
    daysInMonth[1] = 29;
  }

  while (epoch >= (unsigned long)daysInMonth[month - 1]) {
    epoch -= daysInMonth[month - 1];
    month++;
  }

  int day = epoch + 1;
  Serial.printf("%04d-%02d-%02d %02d:%02d:%02d\n", year, month, day, hh, mm, ss);
}

int monthFromAbbrev(const char* month) {
  if (strncmp(month, "Jan", 3) == 0) return 1;
  if (strncmp(month, "Feb", 3) == 0) return 2;
  if (strncmp(month, "Mar", 3) == 0) return 3;
  if (strncmp(month, "Apr", 3) == 0) return 4;
  if (strncmp(month, "May", 3) == 0) return 5;
  if (strncmp(month, "Jun", 3) == 0) return 6;
  if (strncmp(month, "Jul", 3) == 0) return 7;
  if (strncmp(month, "Aug", 3) == 0) return 8;
  if (strncmp(month, "Sep", 3) == 0) return 9;
  if (strncmp(month, "Oct", 3) == 0) return 10;
  if (strncmp(month, "Nov", 3) == 0) return 11;
  return 12;
}

bool isLeapYear(int year) {
  return (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0));
}

unsigned long daysBeforeMonth(int year, int month) {
  static const unsigned short daysInMonth[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  unsigned long days = 0;
  for (int i = 1; i < month; i++) {
    days += daysInMonth[i - 1];
    if (i == 2 && isLeapYear(year)) {
      days += 1;
    }
  }
  return days;
}

unsigned long compileTimeEpochUTC() {
  char buildDate[] = __DATE__;
  char buildTime[] = __TIME__;

  char monthText[4] = { buildDate[0], buildDate[1], buildDate[2], '\0' };
  int month = monthFromAbbrev(monthText);
  int day = atoi(buildDate + 4);
  int year = atoi(buildDate + 7);

  int hour = atoi(buildTime);
  int minute = atoi(buildTime + 3);
  int second = atoi(buildTime + 6);

  unsigned long days = 0;
  for (int y = 1970; y < year; y++) {
    days += isLeapYear(y) ? 366UL : 365UL;
  }

  days += daysBeforeMonth(year, month);
  days += (unsigned long)(day - 1);

  return (days * 86400UL) + ((unsigned long)hour * 3600UL) + ((unsigned long)minute * 60UL) + (unsigned long)second;
}

bool isReasonableEpoch(unsigned long epoch) {
  return epoch >= 1704067200UL && epoch < 4102444800UL;
}

unsigned long getCurrentEpochUTC() {
  if (!ntp_sincronizado) {
    return 0;
  }
  return epochTime + ((millis() - millisAtSync) / 1000);
}

unsigned long getCurrentEpochLocal() {
  long local = (long)getCurrentEpochUTC() + timeZoneOffset;
  if (local < 0) {
    local = 0;
  }
  return (unsigned long)local;
}

void checkReinicioProgramado(unsigned long ahora) {
  unsigned long transcurrido = ahora - inicio_ciclo_reinicio;

  if (ahora - ultimo_log_reinicio >= INTERVALO_LOG_REINICIO_MS) {
    unsigned long faltanteMs = (transcurrido >= INTERVALO_REINICIO_MS) ? 0 : (INTERVALO_REINICIO_MS - transcurrido);
    Serial.print("Reinicio en ");
    Serial.print(faltanteMs / 1000);
    Serial.println(" s");
    ultimo_log_reinicio = ahora;
  }

  if (transcurrido >= INTERVALO_REINICIO_MS) {
    Serial.println("Reinicio programado (4.5 minutos)");
    delay(200);
    reiniciarDispositivo();
  }
}

void reiniciarDispositivo() {
#if defined(ESP32) || defined(ESP8266)
  ESP.restart();
#else
  void (*resetFunc)(void) = 0;
  resetFunc();
#endif
}

// ================= STATUS LED FUNCTIONS =================
void pulseStatusLED() {
  digitalWrite(LED_STATUS, HIGH);
  delay(LED_PULSE_MS);
  digitalWrite(LED_STATUS, LOW);
}

// ================= SENSOR FUNCTIONS =================
void readSensors() {
  readPZEM(sensor1, pzem1);
  readPZEM(sensor2, pzem2);
}

void readPZEM(SensorData &sensor, PZEM004Tv30 &pzem) {
  sensor.voltage = pzem.voltage();
  sensor.current = pzem.current();
  sensor.power = pzem.power();
  sensor.energy = pzem.energy();
  sensor.frequency = pzem.frequency();
  sensor.pf = pzem.pf();

  validateSensorData(sensor);
}

void validateSensorData(SensorData &sensor) {
  if (isnan(sensor.voltage)) sensor.voltage = 0;
  if (isnan(sensor.current)) sensor.current = 0;
  if (isnan(sensor.power)) sensor.power = 0;
  if (isnan(sensor.energy)) sensor.energy = 0;
  if (isnan(sensor.frequency)) sensor.frequency = 0;
  if (isnan(sensor.pf)) sensor.pf = 0;
}

// ================= SERIAL PRINT FUNCTIONS =================
void printSerialReadings() {
  Serial.println("\n========== LECTURAS (15s) ==========");
  printSensorData("PZEM #1", sensor1);
  Serial.println();
  printSensorData("PZEM #2", sensor2);
  Serial.println("===================================");
}

void printSensorData(const char* label, const SensorData &sensor) {
  Serial.println(label);
  Serial.printf("  Voltaje:     %.2f V\n", sensor.voltage);
  Serial.printf("  Corriente:   %.3f A\n", sensor.current);
  Serial.printf("  Potencia:    %.2f W\n", sensor.power);
  Serial.printf("  Energia:     %.2f Wh\n", sensor.energy);
  Serial.printf("  Frecuencia:  %.2f Hz\n", sensor.frequency);
  Serial.printf("  Factor Pot:  %.2f\n", sensor.pf);
}

// ================= MQTT FUNCTIONS =================
bool isEthernetConnected() {
  return Ethernet.linkStatus() != LinkOFF;
}

bool checkMQTTTransport() {
  if (!isEthernetConnected()) {
    Serial.println("No hay enlace Ethernet para MQTT");
    return false;
  }

  return true;
}

String buildMQTTTopic() {
  String topic = "energia/";
  topic += ID_CLIENTE;
  topic += "/";
  topic += ID_DISPOSITIVO;
  topic += "/metricas";
  return topic;
}

String getFormattedDateTime() {
  unsigned long epoch = getCurrentEpochLocal();

  int ss = epoch % 60;
  epoch /= 60;
  int mm = epoch % 60;
  epoch /= 60;
  int hh = epoch % 24;
  epoch /= 24;

  int year = 1970;
  int daysInYear = 365;
  while (epoch >= (unsigned long)daysInYear) {
    epoch -= daysInYear;
    year++;
    daysInYear = (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ? 366 : 365;
  }

  int month = 1;
  int daysInMonth[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) {
    daysInMonth[1] = 29;
  }

  while (epoch >= (unsigned long)daysInMonth[month - 1]) {
    epoch -= daysInMonth[month - 1];
    month++;
  }

  int day = epoch + 1;

  char buffer[20];
  snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d %02d:%02d:%02d", year, month, day, hh, mm, ss);
  return String(buffer);
}

String buildSensorPhaseJSON(int fase, const SensorData &sensor, const String &timestamp) {
  String json = "{";
  json += "\"FechaMedicion\":\"" + timestamp + "\",";
  json += "\"Fase\":" + String(fase) + ",";
  json += "\"Voltaje\":\"" + String(sensor.voltage, 6) + "\",";
  json += "\"Corriente\":\"" + String(sensor.current, 6) + "\",";
  json += "\"Potencia\":\"" + String(sensor.power, 6) + "\",";
  json += "\"FactorPotencia\":\"" + String(sensor.pf, 6) + "\",";
  json += "\"Frecuencia\":\"" + String(sensor.frequency, 6) + "\",";
  json += "\"EnergiaActiva\":\"" + String(sensor.energy, 6) + "\"";
  json += "}";
  return json;
}

String buildMQTTPayload() {
  String payload;
  payload.reserve(768);

  String timestamp = getFormattedDateTime();

  payload = "{\"metricas\":[";
  payload += buildSensorPhaseJSON(2, sensor2, timestamp) + ",";
  payload += buildSensorPhaseJSON(1, sensor1, timestamp);
  payload += "]}";

  return payload;
}

bool connectMQTT() {
  if (!checkMQTTTransport()) {
    return false;
  }

  if (sslClient.connected()) {
    Serial.println("SSL ya esta conectado, cerrando...");
    sslClient.stop();
    delay(1000);
  }

  String clientId = String(MQTT_CLIENT_ID_PREFIX);

  Serial.print("Conectando MQTT a AWS IoT Core: ");
  Serial.println(AWS_IOT_ENDPOINT);
  Serial.print("Client ID: ");
  Serial.println(String(ID_CLIENTE));
  Serial.print("Device ID: ");
  Serial.println(String(ID_DISPOSITIVO));

  bool connected = mqttClient.connect(clientId.c_str());
  if (!connected) {
    Serial.print("Fallo MQTT, estado PubSubClient: ");
    int state = mqttClient.state();
    Serial.print(state);
    Serial.print(" (");
    switch(state) {
      case -4: Serial.print("MQTT_CONNECTION_TIMEOUT"); break;
      case -3: Serial.print("MQTT_CONNECTION_LOST"); break;
      case -2: Serial.print("MQTT_CONNECT_FAILED"); break;
      case -1: Serial.print("MQTT_DISCONNECTED"); break;
      case  1: Serial.print("MQTT_CONNECT_BAD_PROTOCOL"); break;
      case  2: Serial.print("MQTT_CONNECT_BAD_CLIENT_ID"); break;
      case  3: Serial.print("MQTT_CONNECT_UNAVAILABLE"); break;
      case  4: Serial.print("MQTT_CONNECT_BAD_CREDENTIALS"); break;
      case  5: Serial.print("MQTT_CONNECT_UNAUTHORIZED"); break;
      default: Serial.print("UNKNOWN");
    }
    Serial.println(")");

    if (sslClient.getWriteError() != 0) {
      Serial.print("SSL Write Error: ");
      Serial.println(sslClient.getWriteError());
    }

    sslClient.stop();
    return false;
  }

  Serial.println("MQTT conectado exitosamente");
  return true;
}

void maintainMQTTConnection() {
  if (mqttClient.connected()) {
    return;
  }

  if (sslClient.connected() && !mqttClient.connected()) {
    Serial.println("SSL conectado pero MQTT desconectado, cerrando SSL...");
    sslClient.stop();
    delay(500);
  }

  unsigned long ahora = millis();
  if (ahora - ultimo_intento_mqtt < INTERVALO_REINTENTO_MQTT) {
    return;
  }

  ultimo_intento_mqtt = ahora;
  connectMQTT();
}

void publishMQTTMessage() {
  if (!ntp_sincronizado) {
    Serial.println("No se puede publicar MQTT sin hora valida");
    return;
  }

  if (!mqttClient.connected() && !connectMQTT()) {
    Serial.println("No se pudo publicar: MQTT desconectado");
    return;
  }

  String topic = buildMQTTTopic();
  String payload = buildMQTTPayload();

  Serial.println("Publicando MQTT...");
  Serial.print("Topic: ");
  Serial.println(topic);
  Serial.print("Payload: ");
  Serial.println(payload);

  bool ok = mqttClient.publish(topic.c_str(), payload.c_str(), false);
  if (ok) {
    Serial.println("Publicacion MQTT OK");
  } else {
    Serial.print("Error al publicar MQTT, estado: ");
    Serial.println(mqttClient.state());
  }
}

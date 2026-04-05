#include <SPI.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#include <ArduinoHttpClient.h>
#include <SSLClient.h>
#include <PZEM004Tv30.h>
#include <time.h>
#include "arduino_secrets.h"

// ================= CONFIG =================
const char* token_api = TOKEN;
#define ETH_CS 5
#define RNG_PIN 34
#define LED_STATUS 4

#include "cert.h"

// https://github.com/OPEnSLab-OSU/SSLClient/tree/master

// ================= ETHERNET CONFIG =================
byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
const char server[] = "api.alephsub0.org";
const int port = 443;
const char path[] = "/v1/daniel_lara/iot-fuente-lotes/";

// ================= TIMING CONFIG =================
const unsigned long INTERVALO_SERIAL = 15000;
const unsigned long INTERVALO_ENVIO = 60000;
const unsigned long LED_PULSE_MS = 50;
const unsigned long LOOP_DELAY_MS = 500;

// ================= TIMING VARIABLES =================
unsigned long ultimo_serial = 0;
unsigned long ultimo_envio = 0;
int ultimo_minuto_enviado = -1;
bool ntp_sincronizado = false;
unsigned long ultimo_intento_ntp = 0;
const unsigned long INTERVALO_REINTENTO_NTP = 30000;

// ================= PZEM HARDWARE =================
HardwareSerial PZEMSerial1(2);
PZEM004Tv30 pzem1(PZEMSerial1, 16, 17);

HardwareSerial PZEMSerial2(1);
PZEM004Tv30 pzem2(PZEMSerial2, 25, 26);

// ================= ETHERNET CLIENTS =================
EthernetClient ethClient;
EthernetUDP udp;
SSLClient sslClient(ethClient, TAs, sizeof(TAs) / sizeof(TAs[0]), RNG_PIN, 4096, SSLClient::SSL_WARN);
HttpClient client(sslClient, server, port);

// ================= NTP CONFIG =================
const char* ntpServer = "pool.ntp.org";
const int NTP_PACKET_SIZE = 48;
byte packetBuffer[NTP_PACKET_SIZE];
unsigned long epochTime = 0;
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

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(500);
  
  initLED();
  initEthernet();
  initPZEM();
  
  // Intentar sincronizar hora con NTP
  intentarSincronizarNTP();
  
  Serial.println("\n========== SISTEMA LISTO ==========\n");
}

// ================= LOOP =================
void loop() {
  unsigned long ahora = millis();
  
  pulseStatusLED();
  
  // Reintentar sincronización NTP si no está sincronizado
  if (!ntp_sincronizado && (ahora - ultimo_intento_ntp >= INTERVALO_REINTENTO_NTP)) {
    intentarSincronizarNTP();
    ultimo_intento_ntp = ahora;
  }
  
  if (ahora - ultimo_serial >= INTERVALO_SERIAL) {
    readSensors();
    printSerialReadings();
    ultimo_serial = ahora;
  }
  
  // Sistema de envío con dos modos: NTP (si está sincronizado) o millis (fallback)
  if (ntp_sincronizado) {
    // Modo NTP: enviar al inicio de cada minuto
    unsigned long currentTime = epochTime + (millis() / 1000);
    int segundos = currentTime % 60;
    int minuto_actual = (currentTime / 60) % 60;
    
    if (minuto_actual != ultimo_minuto_enviado && segundos < 5) {
      readSensors();
      sendHTTPRequest();
      ultimo_minuto_enviado = minuto_actual;
    }
  } else {
    // Modo fallback: enviar cada 60 segundos usando millis()
    if (ahora - ultimo_envio >= INTERVALO_ENVIO) {
      readSensors();
      sendHTTPRequest();
      ultimo_envio = ahora;
    }
  }
  
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
    Serial.println("❌ DHCP falló");
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
    Serial.println("❌ Ethernet no conectado");
    return;
  }
  
  Serial.println("✅ Ethernet conectado");
}

void initPZEM() {
  Serial.println("Inicializando PZEMs...");
  delay(500);
  
  PZEMSerial1.begin(9600, SERIAL_8N1, 16, 17);
  PZEMSerial2.begin(9600, SERIAL_8N1, 25, 26);
  
  Serial.println("✅ PZEMs inicializados");
}

void intentarSincronizarNTP() {
  Serial.println("Sincronizando hora con NTP (UDP)...");
  
  udp.begin(8888); // Puerto local para NTP
  
  for (int intento = 0; intento < 3; intento++) {
    if (intento > 0) Serial.print(".");
    
    // Enviar solicitud NTP
    sendNTPpacket(ntpServer);
    
    // Esperar respuesta
    unsigned long startTime = millis();
    while (millis() - startTime < 2000) {
      if (udp.parsePacket()) {
        udp.read(packetBuffer, NTP_PACKET_SIZE);
        
        // Extraer timestamp (segundos desde 1900)
        unsigned long highWord = word(packetBuffer[40], packetBuffer[41]);
        unsigned long lowWord = word(packetBuffer[42], packetBuffer[43]);
        unsigned long secsSince1900 = highWord << 16 | lowWord;
        
        // Convertir a Unix time (segundos desde 1970)
        const unsigned long seventyYears = 2208988800UL;
        epochTime = secsSince1900 - seventyYears + timeZoneOffset;
        
        if (epochTime > 1577836800) { // Validar (después de 2020)
          Serial.println("");
          Serial.print("✅ NTP sincronizado: ");
          printDateTime(epochTime);
          ntp_sincronizado = true;
          udp.stop();
          return;
        }
      }
      delay(10);
    }
  }
  
  Serial.println("");
  Serial.println("⚠️ NTP no disponible - usando modo millis()");
  Serial.println("   El sistema funcionará normalmente, pero sin timestamps exactos");
  ntp_sincronizado = false;
  udp.stop();
}

void sendNTPpacket(const char* address) {
  memset(packetBuffer, 0, NTP_PACKET_SIZE);
  packetBuffer[0] = 0b11100011;   // LI, Version, Mode
  packetBuffer[1] = 0;            // Stratum
  packetBuffer[2] = 6;            // Polling Interval
  packetBuffer[3] = 0xEC;         // Peer Clock Precision
  packetBuffer[12] = 49;
  packetBuffer[13] = 0x4E;
  packetBuffer[14] = 49;
  packetBuffer[15] = 52;
  
  udp.beginPacket(address, 123); // Puerto NTP: 123
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
  
  int dayOfWeek = (epoch + 4) % 7;
  
  int year = 1970;
  int daysInYear = 365;
  while (epoch >= daysInYear) {
    epoch -= daysInYear;
    year++;
    daysInYear = (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ? 366 : 365;
  }
  
  int month = 1;
  int daysInMonth[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  if (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) daysInMonth[1] = 29;
  
  while (epoch >= daysInMonth[month - 1]) {
    epoch -= daysInMonth[month - 1];
    month++;
  }
  
  int day = epoch + 1;
  
  Serial.printf("%04d-%02d-%02d %02d:%02d:%02d\n", year, month, day, hh, mm, ss);
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
  if (isnan(sensor.voltage))   sensor.voltage = 0;
  if (isnan(sensor.current))   sensor.current = 0;
  if (isnan(sensor.power))     sensor.power = 0;
  if (isnan(sensor.energy))    sensor.energy = 0;
  if (isnan(sensor.frequency)) sensor.frequency = 0;
  if (isnan(sensor.pf))        sensor.pf = 0;
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
  Serial.printf("  Energía:     %.2f Wh\n", sensor.energy);
  Serial.printf("  Frecuencia:  %.2f Hz\n", sensor.frequency);
  Serial.printf("  Factor Pot:  %.2f\n", sensor.pf);
}

// ================= HTTP REQUEST FUNCTIONS =================

void sendHTTPRequest() {
  Serial.print("\n📡 Enviando POST HTTPS ");
  
  if (ntp_sincronizado) {
    unsigned long currentTime = epochTime + (millis() / 1000);
    Serial.print("[NTP: ");
    printDateTime(currentTime);
    Serial.println("]...");
  } else {
    Serial.println("[millis: cada 60s]...");
  }
  
  digitalWrite(LED_STATUS, HIGH);
  
  String payload = buildJSONPayload();
  
  if (!isEthernetConnected()) {
    Serial.println("❌ Ethernet desconectado");
    digitalWrite(LED_STATUS, LOW);
    return;
  }
  
  sendPayload(payload);
  digitalWrite(LED_STATUS, LOW);
}

String buildJSONPayload() {
  String payload;
  payload.reserve(512);
  
  payload = "{\"datos\":[";
  payload += buildSensorJSON(2, sensor1.voltage) + ",";
  payload += buildSensorJSON(3, sensor1.current) + ",";
  payload += buildSensorJSON(4, sensor1.power) + ",";
  payload += buildSensorJSON(5, sensor1.pf) + ",";
  payload += buildSensorJSON(6, sensor1.frequency) + ",";
  payload += buildSensorJSON(7, sensor1.energy) + ",";
  payload += buildSensorJSON(8, sensor2.voltage) + ",";
  payload += buildSensorJSON(9, sensor2.current) + ",";
  payload += buildSensorJSON(10, sensor2.power) + ",";
  payload += buildSensorJSON(11, sensor2.pf) + ",";
  payload += buildSensorJSON(12, sensor2.frequency) + ",";
  payload += buildSensorJSON(13, sensor2.energy);
  payload += "]}";
  
  Serial.println("Payload:");
  Serial.println(payload);
  
  return payload;
}

String buildSensorJSON(int idSensor, float medicion) {
  return "{\"IdSensor\":" + String(idSensor) + ",\"Medicion\":" + String(medicion, 6) + "}";
}

bool isEthernetConnected() {
  return Ethernet.linkStatus() != LinkOFF;
}

void sendPayload(const String &payload) {
  // Cerrar cualquier conexión previa para evitar errores SSL
  if (sslClient.connected()) {
    sslClient.stop();
    delay(100);
  }
  
  client.beginRequest();
  client.post(path);
  client.sendHeader("Content-Type", "application/json");
  client.sendHeader("Authorization", "Token " + String(token_api));
  client.sendHeader("Content-Length", payload.length());
  client.sendHeader("Connection", "close");
  client.beginBody();
  client.print(payload);
  client.endRequest();
  
  handleHTTPResponse();
  
  // Cerrar la conexión después del envío
  sslClient.stop();
}

void handleHTTPResponse() {
  int statusCode = client.responseStatusCode();
  String response = client.responseBody();
  
  if (statusCode > 0) {
    Serial.print("✅ Lote enviado OK (HTTP ");
    Serial.print(statusCode);
    Serial.println(")");
    if (response.length() > 0) {
      Serial.println("Respuesta:");
      Serial.println(response);
    }
  } else {
    Serial.print("❌ Error envío lote: ");
    Serial.println(statusCode);
  }
}
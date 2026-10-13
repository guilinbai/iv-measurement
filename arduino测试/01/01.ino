void setup() {
  pinMode(LED_BUILTIN, OUTPUT);      // Nano 板载 LED 在 D13
}
void loop() {
  digitalWrite(LED_BUILTIN, HIGH);   // 亮
  delay(1000);
  digitalWrite(LED_BUILTIN, LOW);    // 灭
  delay(100);
}
/* ============================================================================
 *  Test-D2-D11 : 电路检查用测试程序
 *  D2 ~ D11 依次接通，任意时刻只有一路导通
 *  ---------------------------------------------------------------------------
 *  用法：上传后打开串口监视器（115200），屏幕会依次显示当前点亮哪个引脚；
 *       万用表切【通断档】，表笔搭在对应光耦的 3、4 脚上听蜂鸣。
 *
 *  D2  -> 光耦-1 + 光耦-2    (ch1 选通A/B)
 *  D3  -> 光耦-3             (ch1 短接)
 *  D4  -> 光耦-4 + 光耦-5    (ch2 选通A/B)
 *  D5  -> 光耦-6             (ch2 短接)
 *  D6  -> 光耦-7 + 光耦-8    (ch3 选通A/B)
 *  D7  -> 光耦-9             (ch3 短接)
 *  D8  -> 光耦-10 + 光耦-11  (ch4 选通A/B)
 *  D9  -> 光耦-12            (ch4 短接)
 *  D10 -> 光耦-13            (模式耦合器·电流)
 *  D11 -> 光耦-14            (模式耦合器·电压)
 *
 *  另一个快速判断法：直流电压档量【光耦 1 脚对 GND】
 *    该路点亮时约 1.1 V（LED 正向导通），熄灭时约 0 V。
 *  ==========================================================================*/

const uint8_t PINS[] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
const uint8_t N      = sizeof(PINS) / sizeof(PINS[0]);

const char* LABEL[] = {
  "光耦-1 + 光耦-2    (ch1 选通A/B)",
  "光耦-3             (ch1 短接)",
  "光耦-4 + 光耦-5    (ch2 选通A/B)",
  "光耦-6             (ch2 短接)",
  "光耦-7 + 光耦-8    (ch3 选通A/B)",
  "光耦-9             (ch3 短接)",
  "光耦-10 + 光耦-11  (ch4 选通A/B)",
  "光耦-12            (ch4 短接)",
  "光耦-13            (模式耦合器·电流)",
  "光耦-14            (模式耦合器·电压)"
};

// -------- 速度（想更快改小，想更慢改大）--------
const uint16_t DWELL = 300;   // 每路点亮时长 ms
const uint16_t GAP   = 150;   // 两路之间的全灭间隔 ms

void allOff() {
  for (uint8_t i = 0; i < N; i++) digitalWrite(PINS[i], LOW);
}

void setup() {
  for (uint8_t i = 0; i < N; i++) {
    pinMode(PINS[i], OUTPUT);
    digitalWrite(PINS[i], LOW);
  }
  Serial.begin(115200);
  delay(300);
  Serial.println(F("=== D2~D11 依次接通测试（每次只有一路导通）==="));
}

void loop() {
  for (uint8_t i = 0; i < N; i++) {
    allOff();
    delay(GAP);
    digitalWrite(PINS[i], HIGH);
    Serial.print(F("HIGH  D"));
    Serial.print(PINS[i]);
    Serial.print(F("  ->  "));
    Serial.println(LABEL[i]);
    delay(DWELL);
  }
  allOff();
  delay(GAP);
}

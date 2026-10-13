/* ============================================================================
 *  4 路 IV 测量 —— Arduino Nano 控制固件  v1.1
 *  ---------------------------------------------------------------------------
 *  v1.1 相对 v1.0 的新增：
 *    ① 新增 S 命令：扫一轮（固定 4 路 × V+I = 8 项），按模式分组执行
 *         S              = 全部 8 项
 *         S 1111|1111    = 参数化（竖线前=电压 4 位，后=电流 4 位，1=测 0=跳过）
 *    ② 每个读数窗口主动上报 "WIN <通道> <V|I>"，上位机收到后立刻读表
 *    ③ 一轮结束上报 "ROUND DONE"
 *    ④ 看门狗：10 秒没收到任何命令 → 自动回安全态并报 "WATCHDOG"
 *
 *  接线（见《5-面包板排线教程.md》）：
 *    D2  = 通道1 选通（同时驱动 光耦-1、光耦-2）
 *    D3  = 通道1 短接（光耦-3）
 *    D4  = 通道2 选通   D5 = 通道2 短接   (光耦-4/5/6)
 *    D6  = 通道3 选通   D7 = 通道3 短接   (光耦-7/8/9)
 *    D8  = 通道4 选通   D9 = 通道4 短接   (光耦-10/11/12)
 *    D10 = 模式耦合器·电流（光耦-13）
 *    D11 = 模式耦合器·电压（光耦-14）
 *
 *  电平约定：光耦 LED 高电平 = 导通 = 开关闭合
 *    选通 HIGH = 器件接上万用表；短接 HIGH = 器件两端被短接
 *    安全态 = 选通全 LOW + 短接全 HIGH
 *
 *  ★ 串口协议（115200，行尾 \n）
 *    上位机 → 固件:
 *      S               扫一轮（全部 8 项）
 *      S 1111|1111     扫一轮（自定义掩码）
 *      s               立刻回安全态
 *      c / u           耦合器切到 电流 / 电压 位
 *      i1..i4          手动测一次 通道n 的 Isc
 *      v1..v4          手动测一次 通道n 的 Voc
 *      a               脱机自动循环开关（不接 PC 时自测用）
 *      PING            心跳（喂看门狗）
 *    固件 → 上位机:
 *      READY v1.1      启动完成
 *      WIN 1 V         ★ 通道1 的电压读数窗口已打开（立刻读表）
 *      WIN 1 I         ★ 通道1 的电流读数窗口已打开
 *      ROUND DONE      一轮全部完成
 *      SAFE / OK       状态确认
 *      WATCHDOG        看门狗触发（已回安全态）
 *      ERR <msg>       命令错误
 *
 *  ⚠️ 上传前务必先关闭串口监视器
 *  ==========================================================================*/

const uint8_t PIN_SEL[4]   = {2, 4, 6, 8};
const uint8_t PIN_SHORT[4] = {3, 5, 7, 9};
const uint8_t PIN_MODE_I   = 10;
const uint8_t PIN_MODE_V   = 11;

const uint8_t NCH = 4;        // 通道数

// -------- 微时序（毫秒）--------
uint16_t T_OPEN      = 30;    // 断开短接后，等器件升至开路电压
uint16_t T_SETTLE    = 80;    // 合上选通后，等瞬态衰减
uint16_t T_WINDOW    = 50;    // ★ 读数窗口：上报 WIN 后等上位机读表
uint16_t T_OFF       = 20;    // 离表后、合短接前的空档
uint16_t T_ROUND_GAP = 0;     // 脱机自动循环时的轮间隔
uint16_t T_WATCHDOG  = 10000; // 看门狗超时（ms）：这么久没命令就回安全态

bool     currentMode = true;  // true=耦合器在电流位, false=电压位
bool     autoRun     = false; // 脱机自动循环
uint32_t lastCmdMs   = 0;     // 最近一次收到命令的时间

// ============================================================================
//  基础动作
// ============================================================================
void allSafe() {
  for (uint8_t i = 0; i < NCH; i++) {
    digitalWrite(PIN_SEL[i],   LOW);    // 选通断开
    digitalWrite(PIN_SHORT[i], HIGH);   // 短接闭合
  }
}

// 耦合器切换（break-before-make：绝不同时导通）
void switchMode(bool toCurrent) {
  if (toCurrent == currentMode) return;
  if (toCurrent) {
    digitalWrite(PIN_MODE_V, LOW);
    delay(10);
    digitalWrite(PIN_MODE_I, HIGH);
  } else {
    digitalWrite(PIN_MODE_I, LOW);
    delay(10);
    digitalWrite(PIN_MODE_V, HIGH);
  }
  currentMode = toCurrent;
  delay(20);
}

// 单次测量窗口：断短接 → 合选通 → 报 WIN → 等读数 → 断选通 → 合短接
void measureWindow(uint8_t ch, char mode) {
  digitalWrite(PIN_SHORT[ch], LOW);      // ① 先断短接
  delay(T_OPEN);

  digitalWrite(PIN_SEL[ch], HIGH);       // ② 再合选通
  delay(T_SETTLE);

  Serial.print(F("WIN "));               // ③ 上报窗口，上位机立刻读表
  Serial.print(ch + 1);
  Serial.print(' ');
  Serial.println(mode);
  delay(T_WINDOW);

  digitalWrite(PIN_SEL[ch], LOW);        // ④ 先断选通
  delay(T_OFF);
  digitalWrite(PIN_SHORT[ch], HIGH);     // ⑤ 再合短接
  delay(10);
}

// ============================================================================
//  核心：扫一轮（按模式分组，耦合器一轮只切 2 次）
// ============================================================================
void scanRound(uint8_t maskV, uint8_t maskI) {
  // ---- 第一段：电压组 ----
  if (maskV) {
    switchMode(false);                                 // 耦合器切到电压位
    for (uint8_t ch = 0; ch < NCH; ch++) {
      if (maskV & (1 << ch)) measureWindow(ch, 'V');
    }
  }

  // ---- 切换 ----
  if (maskI) switchMode(true);                         // 电压位 → 电流位

  // ---- 第二段：电流组 ----
  if (maskI) {
    for (uint8_t ch = 0; ch < NCH; ch++) {
      if (maskI & (1 << ch)) measureWindow(ch, 'I');
    }
  }

  // ---- 收尾：耦合器回电压位，为下一轮准备 ----
  switchMode(false);

  Serial.println(F("ROUND DONE"));
}

// 解析掩码字符串，如 "1111|1111" → maskV=0x0F, maskI=0x0F
bool parseMask(String s, uint8_t *maskV, uint8_t *maskI) {
  int bar = s.indexOf('|');
  if (bar < 0) return false;
  String sv = s.substring(0, bar);
  String si = s.substring(bar + 1);
  if (sv.length() < NCH || si.length() < NCH) return false;

  *maskV = 0; *maskI = 0;
  for (uint8_t i = 0; i < NCH; i++) {
    if (sv.charAt(i) == '1') *maskV |= (1 << i);
    if (si.charAt(i) == '1') *maskI |= (1 << i);
  }
  return true;
}

// ============================================================================
//  串口命令处理
// ============================================================================
void handleSerial() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  lastCmdMs = millis();                      // 喂看门狗
  char c = cmd.charAt(0);

  if (c == 'S') {                            // ★ 扫一轮
    uint8_t maskV = 0x0F, maskI = 0x0F;
    if (cmd.length() > 1) {
      String arg = cmd.substring(1);
      arg.trim();
      if (arg.length() > 0) {
        if (!parseMask(arg, &maskV, &maskI)) {
          Serial.println(F("ERR bad mask (use: S 1111|1111)"));
          return;
        }
      }
    }
    scanRound(maskV, maskI);
  }
  else if (c == 'a') {                       // 脱机自动循环（不接 PC 时自测）
    autoRun = !autoRun;
    if (!autoRun) allSafe();
    Serial.println(autoRun ? F("OK auto run START") : F("SAFE auto run STOP"));
  }
  else if (c == 'c') { switchMode(true);  Serial.println(F("OK CURRENT mode")); }
  else if (c == 'u') { switchMode(false); Serial.println(F("OK VOLTAGE mode")); }
  else if (c == 's') { autoRun = false; allSafe(); Serial.println(F("SAFE")); }
  else if (c == 'P' || c == 'p') { Serial.println(F("OK PONG")); }   // 心跳
  else if ((c == 'i' || c == 'v') && cmd.length() >= 2) {
    uint8_t ch = cmd.charAt(1) - '1';
    if (ch >= NCH) { Serial.println(F("ERR channel must be 1~4")); return; }
    if (c == 'i') { switchMode(true);  measureWindow(ch, 'I'); }
    else          { switchMode(false); measureWindow(ch, 'V'); }
    Serial.println(F("OK done"));
  }
  else {
    Serial.println(F("ERR unknown cmd (S / s / c / u / i1..i4 / v1..v4 / a / PING)"));
  }
}

// ============================================================================
void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < NCH; i++) {
    pinMode(PIN_SEL[i],   OUTPUT);
    pinMode(PIN_SHORT[i], OUTPUT);
    digitalWrite(PIN_SEL[i],   LOW);
    digitalWrite(PIN_SHORT[i], HIGH);
  }
  pinMode(PIN_MODE_I, OUTPUT);
  pinMode(PIN_MODE_V, OUTPUT);

  allSafe();
  currentMode = true;
  digitalWrite(PIN_MODE_I, HIGH);
  digitalWrite(PIN_MODE_V, LOW);
  switchMode(false);                 // 默认落在电压位

  lastCmdMs = millis();
  delay(300);

  Serial.println(F("READY v1.1"));
  Serial.println(F("cmds: S [mask] / s / c / u / i1..i4 / v1..v4 / a / PING"));
}

// ============================================================================
void loop() {
  handleSerial();

  // ---- 看门狗：太久没命令 → 回安全态 ----
  if (millis() - lastCmdMs > T_WATCHDOG) {
    lastCmdMs = millis();
    autoRun = false;
    allSafe();
    Serial.println(F("WATCHDOG"));
  }

  // ---- 脱机自动循环（不接 PC 时自测用）----
  if (autoRun) {
    scanRound(0x0F, 0x0F);
    if (T_ROUND_GAP) delay(T_ROUND_GAP);
  }
}

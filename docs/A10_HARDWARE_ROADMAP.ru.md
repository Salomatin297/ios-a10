# A10 (SM-A105F, Exynos 7884B): железные подсистемы — инвентарь, контракты, план

Документ от 2026-11-13. Цель: зафиксировать **фактическое** состояние по Wi-Fi,
Bluetooth, Cellular и звуку — что есть в железе, что есть в iOS-userland, чего
не хватает и в каком порядке это разумно делать. Все адреса и имена взяты из
живого DT стокового Android (`dist/A10/fdt.dtb`) и из содержимого iOS-тома
(каталог HFS+), а не из предположений.

---

## 0. Главный вывод (архитектурный)

iOS-userland **не содержит** драйверов этих подсистем: он ожидает
проприетарные Apple-драйверы, которые живут в ядре iOS (кепты) или в
проприетарных модулях DriverKit.

| Подсистема | Что нужно iOS-userland | Есть ли в нашем томе/ядре |
|---|---|---|
| Wi-Fi | `IO80211.framework` ↔ **AppleWiFi.kext** (закрытый кепт iOS) | ❌ кепта нет; `AppleWiFi` в томе не найден |
| Bluetooth | `MobileBluetooth.framework` ↔ **AppleBluetooth** (закрытый) | ❌ нет |
| Cellular | `CommCenter` (кернел + `CoreTelephony`) | ⚠️ в томе только `com.apple.CommCenter-noncellular.plist` и хелперы; кепта `CommCenter` нет |
| Звук | `AppleAOPAudioPlugin.driver` (HAL) ↔ **ядерный сервис «AOP audio»** | ✅ HAL-плагин **есть в томе**, ядерного сервиса нет |

Следствия:

* **Wi-Fi/BT/Cellular в лоб не поднимаются**: без `AppleWiFi`/`AppleBluetooth`
  стек iOS не поднимется ни на каком чипе. Нужен либо собственный IOKit-сервис
  с тем же контрактом (реверс `AppleWiFi` невозможен — кепта нет), либо
  «свой» стек, который iOS-приложения использовать не будут.
* **Звук — единственная подсистема, где контракт доступен**: HAL-плагин лежит в
  томе, его строки и имена методов читаются. Значит можно реализовать
  ядерный сервис с тем же поведением и получить штатный звук iOS.

Поэтому порядок работ: **звук → (Wi-Fi как эксперимент) → BT → Cellular**.

---

## 1. Звук

### 1.1 Железо (живой DT `dist/A10/fdt.dtb`)

| Блок | Адрес | Детали |
|---|---|---|
| **ABOX** (audio block) | `0x14A50000` | свой GIC `abox_gic@0x14AF0000` (`0x14af1000` GICD, `0x14af2000` GICC), IOMMU `sysmmu_abox@0x14A30000` (IRQ 129), reserved-memory `abox_rmem` `0xE9400000` 4 МБ, `btabox_rmem` 4 МБ |
| **SPEEDY** (периферийная шина) | `0x11CE0000` (0x2000) | `samsung,exynos-speedy`, IRQ 34, clock `gate_speedy`, pinctrl `speedy-bus` (gpq0-2) |
| **COD3035X** (кодек) | SPEEDY-устройство `0x03` | `codec,cod3035x`, `i2c-speedy-address`, mic-bias 1/2, mic-bias LDO |
| Звуковая карта | `/sound` | `samsung,exynos7885-cod3035`, ссылки rdma0-7, wdma0-4, uaif0-3, `internal@0` → cpu=`/abox@0x14A50000`, codec=`/audio_codec_dummy` (в Android внутренний кодек идёт «в dummy», реальный обмен — через SPEEDY) |
| Прочее | pinctrl `aud-codec-mclk` (gpb0-0 fn 2), `aud-codec-bus` (gpb0-1/gpb0-2/gpb0-3/gpb0-4 fn 2), `aud_dmic_on/off` | цифровой микрофон |

Вывод: это **не** «I2S + простой кодек». ABOX — отдельный звуковой блок с
собственным GIC и IOMMU, у него свой прошитый микроконтроллер (AOP), а кодек
висит на шине SPEEDY. Старые драйверы Exynos (i2s + wm8994) здесь не применимы.

### 1.2 Контракт со стороны iOS (из HAL-плагина в томе)

Файл: `/System/Library/Audio/Plug-Ins/HAL/AppleAOPAudioPlugin.driver/AppleAOPAudioPlugin`
(264 256 байт, вытащен из тома, `work/AppleAOPAudioPlugin.bin`).

Из строк бинарника:

* классы `AppleAOPAudioPlugin`, `AOPAudioDeviceHWManager`, `BorealisOwl_Device`;
* плагин ищет **IOKit-сервис «Audio Service»** через `IOServiceMatching` +
  `IOServiceAddMatchingNotification` («_StartDeviceListNotifications: IOServiceMatching for Audio Service failed»);
* открывает **user client** и зовёт методы:
  `HW_Open`, `HW_StartIO`, `HW_StopIO`, `HW_AcquireRingBuffer`
  (маппинг **ring buffer** и **status buffer** в userspace),
  `HW_AddVoiceTriggerSupport`, `HW_GetVoiceTriggerData`,
  `HW_VoiceTriggerGet/SetControlValue`;
* читает из IORegistry свойства: `BytesPerSample`, `ChannelsPerFrame`,
  `FramesPerPacket`, `InputSamplesPerSec`, `ZeroTimeStampWrapFrames`,
  `SafetyOffsetFrames`, `DeviceInputLatencyFrames`, `IsIORunning`,
  `EnableListeningOnGesture`;
* требует entitlement `com.apple.private.audio.hal.aop-audio.user-access`.

### 1.3 Что нужно сделать (по шагам)

1. **Диагностика шины (питч 1, риск низкий) — сделано, патч 0130.**
   Драйвер `bsd/dev/a10_audio.c`:
   маппинг SPEEDY `0x11CE0000` и ABOX `0x14A50000`, чтение ID кодека через
   SPEEDY, лог в персистентный лог. Цель — убедиться, что шина отвечает и
   кодек виден. Это ~150 строк и один CI-цикл.
2. **Конфигурация кодека и ABOX** (питч 2): маршрутизация I2S-подобного
   потока ABOX → SPEEDY → COD3035X, включение усилителя, выбор
   источника (динамик/гарнитура). Нужны регистры из стокового драйвера
   Samsung (`cod3035`, `exynos-speedy`, `abox`) — их в проекте нет, нужно
   добыть из стокового ядра/исходников.
3. **Ядерный сервис «AOP audio»** (питч 3): класс IOKit с теми же свойствами
   и user client с методами `HW_Open/StartIO/StopIO/AcquireRingBuffer`,
   кольцевой буфер в userspace, чтобы HAL-плагин подхватил устройство.
4. **Entitlement**: выдать `com.apple.private.audio.hal.aop-audio.user-access`
   плагину (в порту AMFI это делается патчем 0035/0036).
5. Проверка: в логе `AppleAOPAudioPlugin` должен перестать писать
   «IOServiceMatching for Audio Service failed», а `coreaudiod` — показать
   устройство вывода.

### 1.4 Риски

* ABOX — проприетарный Samsung-блок с собственным GIC и микроконтроллером;
  протокол обмена с ним (регистры/сообщения) закрыт. Шаг 1 даст ответ
  («шина жива/не жива»), шаги 2-3 могут потребовать месяц реверса.
* Даже если сервис совпадёт по имени, HAL ожидает точные значения
  user-client методов (селекторы) — их придётся снять дизассемблированием
  плагина.

---

## 2. Wi-Fi

* В томе: `IO80211.framework` (private framework), `IO80211DriverKit.framework`
  (DriverKit), `WirelessDiagnostics`, `WirelessProximity`, `WirelessCoexManager`.
* Ядерной части нет: ни `AppleWiFi.kext`, ни его аналога в
  `/System/Library/Extensions` (там только HID/USB/AKS-расходники).
* Железо (уточнено 2026-11-13): Wi-Fi/BT-чип — **SCSC** (Samsung System Chip),
  а не Broadcom. В живом DT: хост-блок `/scsc_wifibt@120c0000`
  (`samsung,scsc_wifibt`) с двумя окнами 0x120c0000 и 0x120e0000 (0x180) и
  прерываниями MBOX(57)/ALI(21)/WEDONE(286)/DOG(42) — это фирменный
  «MBOX/ALI»-интерфейс SCSC; плюс `reserved-memory wifibt_if`
  `0xe9000000` (4 МБ) — общий SRAM с чипом, куда грузится его firmware.
  Драйвера SCSC в XNU нет; в Android он грузит firmware в этот SRAM и
  работает по MBOX/ALI. Отдельно нужен 802.11-стек: в XNU нет аналога
  mac80211 вообще.
* Практический вывод: **Wi-Fi в iOS-userland не заработает** без закрытого
  `AppleWiFi`. Варианты:
  1. свой IOKit-сервис с контрактом `AppleWiFi` (реверс невозможен — кепта
     нет в томе; можно попытаться по публичным символам/стокам, но это
     многомесячная работа);
  2. «параллельный» стек: реализовать в ядре brcmfmac-подобный стек и
     отдавать сеть iOS через **USB/ETHERNET** (у порта уже есть USB-ECM,
     патч 0075) — тогда Wi-Fi-чип даёт сеть на уровне ядра, а iOS видит
     обычный Ethernet-интерфейс. Это реалистичный, хоть и не «честный»
     Wi-Fi-путь: iOS-приложения получат сеть, а настройки Wi-Fi в iOS не
     появятся.
  3. не делать в этом цикле.

---

## 3. Bluetooth

* В томе: `CoreBluetooth.framework`, `MobileBluetooth.framework`,
  `BluetoothServices`, `BTAudioHALPlugin.plugin`, `BluetoothUIService.app`,
  `bluetoothd` (userland).
* Ядерной части нет: закрытый `AppleBluetooth`/HCI-транспорт отсутствует.
* Практический вывод: то же, что для Wi-Fi — либо свой HCI-транспорт в ядре
  (тогда iOS Bluetooth-стек его не увидит), либо Bluetooth-устройства через
  USB-BT (обычно нет dongle'а).

---

## 4. Cellular

* В томе: `com.apple.CommCenter-noncellular.plist`, `CommCenter`,
  `CommCenterMobileHelper`, `CommCenterRootHelper`, `WirelessModemSettings.bundle`.
* Модем Exynos (SHANNON) физически есть, но: init-прошивка модема не заведена,
  AT-порт не открыт, `CommCenter` (кернел) отсутствует. В iOS модемом
  управляет проприетарный стек с бинарными протоколами Qualcomm/Shannon.
* Практический вывод: **реалистично только после появления рабочей сети
  (Wi-Fi или USB)**, и даже тогда нужен собственный firmware-loader и AT-порт.
  Порядок: сначала сеть через USB/Wi-Fi, потом модем.

---

## 5. Сводка по приоритетам и стоимости

| # | Задача | Что нужно | Реалистичность | Оценка |
|---|---|---|---|---|
| 1 | Звук, шаг 1 (диагностика ABOX/SPEEDY) | драйвер в bsd/dev, CI-цикл | ✅ высокая | 1 цикл |
| 2 | Звук, шаг 2-3 (кодек + сервис «AOP audio») | регистры Samsung-драйверов, реверс user-client методов | ⚠️ средняя | недели |
| 3 | Wi-Fi как сеть через USB-ECM | SCSC-драйвер (MBOX/ALI) + firmware + 802.11-стек в ядре | ⚠️ средняя (не «Wi-Fi» для iOS) | недели |
| 4 | Wi-Fi/BT «как в iOS» | закрытые кепты Apple | ❌ невозможно без исходников | — |
| 5 | Cellular | firmware модема, AT-порт, CommCenter | ❌ низкая | месяцы |

Промежуточный результат, который имеет смысл делать сейчас: **шаг 1 звука**
(проверка живости ABOX/SPEEDY) — он даёт понимание, насколько блок вообще
доступен из XNU, и служит основой для следующих шагов.

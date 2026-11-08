# A10: прогноз первого запуска iOS 16 и матрица драйверов

Дата: 2026-11-08. Состояние: M2 подтверждён, M3 (HFSX root + launchd) готов
к записи, но ещё не проверен на устройстве.

## 1. Что уже доказано

* XNU-8796 стартует на Exynos 7884B в EL1, работает GICv2, generic timer,
  4K-страницы и framebuffer console 720x1600.
* `bsd_init`/`kminit` проходят, `bsd_autoconf` поднимает `lo0`, `gif0`, `stf0`.
* DW-MMC читает CID/EXT_CSD/MBR/GPT через 64-битный FIFO; probe15 открыл
  `emmcroot0` (GPT `userdata`, `bdev=1,0`) и дошёл до `hfs_mountfs`.
* 20 GiB HFSX-образ собран из iPhone X IPSW 16.7.16 (20H392), fsck чистый,
  занято 11 GiB, владельцы `0:80`/`0:0`.
* `a10_write_root.zip` и `a10_probe16.zip` (`emmcw=1 rootrw=1`) готовы.

Этого достаточно, чтобы первый запуск дошёл до монтирования root и, скорее
всего, до `launchd`. Всё, что выше, — прогноз, а не гарантия.

## 2. Что произойдёт после записи образа

Ожидаемая цепочка:

```
SBOOT -> shim -> XNU -> IOKit platform expert -> bsd_init
  -> emmcroot0 -> HFSX mount -> /sbin/launchd
     -> launchd.plist -> runningboardd -> backboardd -> SpringBoard/Setup
```

По экрану:

1. Текст kernel console (как в probe13/15), затем `BSD root: emmcroot0`.
2. Строки HFSX mount и запуск `launchd`; консоль может остаться сверху.
3. Через минуты — чёрный экран, Apple logo, спиннер или Setup «Hello».
4. При ошибке — паника XNU или зависание; снимать экран фото.

Первые несколько минут будут очень медленными: сейчас `cpus=1`, активен
один A73 (probe16), а iOS 16 рассчитан на 6 ядер A11 и 3 ГБ RAM.

## 3. Главные риски, не связанные с драйверами

### 3.1 Образ «чистый», без userland-твиков sweet

`tools/mkiosroot-staged.sh` копирует OS/SystemOS/AppOS и добавляет только
keybag и маркеры logd. В образе **нет** того, что sweet v14 делал для
нормального первого запуска:

* пропуска Setup (`com.apple.purplebuddy.plist`: `SetupDone`,
  `SetupFinishedAllSteps`, `SetupVersion`);
* seed активации `mobileactivationd` (data ark, `ActivationState=Activated`);
* удаления job'ов CommCenter (модема нет);
* watchdog-исключений SpringBoard/Setup;
* отключения `VirtualAudio.plugin`;
* маркера завершённой миграции (`LastSystemVersion`/disposition).

Поэтому наиболее вероятный сценарий первого запуска — не Home Screen, а
Setup/активация, миграция данных или циклический перезапуск демонов. Это
нормально для первого прогона: мы увидим, где именно останавливается iOS.
Для получения рабочего GUI эти твики придётся внести в образ (или в
отдельный post-write слой).

### 3.2 В ADT нет узла `/product`

`mkadt-a10.py` ищет `tools/research/d22ap.adt`, но файла нет, поэтому
`/product` не создаётся. В образе есть только root `model=iPhone10,3`,
`target-type=D22`, `compatible=D22AP/iPhone10,3`. Часть iOS-сервисов
(MobileGestalt, backboardd, identity) может ожидать `/product` и вести себя
как на неизвестном устройстве. Для следующего образа нужно выдернуть
`DeviceTree.d22ap.im4p` из IPSW и положить `d22ap.adt` в `tools/research/`.

### 3.3 2 ГБ RAM против 3 ГБ у iPhone X

SpringBoard + backboardd + Setup + миграция легко превышают 2 ГБ. Вероятны
jetsam-kill'ы и повторные запуски. Это лечится уменьшением числа демонов,
watchdog-исключениями и отключением ненужных job'ов, но не драйверами.

### 3.4 Логический экран 1125x2436

`SweetFramebuffer` представляет панель как iPhone X: iOS рисует 1125x2436,
а на панель 720x1600 выводится центральный кроп. Это 2,74 млн пикселей на
кадр вместо 1,15 млн. Для одного CPU это очень дорого. `sweetfb_native=1`
переключает на 720x1600, но может сломать layout статус-бара.

## 4. Матрица драйверов

| Устройство | Статус | Что сделано / что нужно | Сложность |
|---|---|---|---|
| Экран (S6D7AT0B, DECON/DSIM) | ✅ работает | boot framebuffer 0xEC000000, AppleCLCD, CPU-презентация; нет управления яркостью и native panel init | средняя |
| Кнопки (GPA1.5/6/7) | 🚧 patch 0096 | polled GPIO, active low, `sweet_hid_key`; нужен тест на устройстве | низкая |
| Батарея (S2MU005) | 🚧 patch 0097 | polled I2C3/I2C4, RVBAT/RSOC/RCUR, charger status; read-only; включается `a10battery=1` | низкая–средняя |
| Тачскрин (sec_ts, HSI2C) | ❌ нет | нужен HSI2C/USI, reset/power, прошивка `s6d7at0b01_a10.fw`; firmware нет в kernel-ref, её надо достать из stock vendor | высокая |
| Wi-Fi/BT (SCSC) | ❌ нет | Samsung SCSC, не Broadcom; своя firmware и большой стек | очень высокая |
| Сотовая связь (Shannon s327ap) | ❌ нет | modem_if, CP boot, прошивка, CMUX; фактически отдельный проект | очень высокая |
| Аудио (ABox + COD3035X + MAX98506) | ❌ нет | ABox DSP, codec, маршрутизация; нужен свой audio driver | очень высокая |
| Камеры (S5K3L6 + S5K5E9, FIMC-IS) | ❌ нет | FIMC-IS, I2C, MIPI CSI, firmware; две камеры | очень высокая |
| GPU (Mali-G71 MP2) | ❌ нет и не планируется | AGX несовместим с Mali; см. раздел 5 | недостижимо |
| 2D-ускорение (DECON) | 💡 возможно | zero-copy scanout IOSurface или overlay-композиция вместо CPU-копии | средняя |

### 4.1 Кнопки

Патч 0096 добавляет `bsd/dev/a10_keys.c`:

* читает `pinctrl@11CB0000`, GPA1 CON `0x060`/DAT `0x064`;
* переводит пины 5/6/7 в input (как stock gpio-keys), если не задан
  `a10keys_keepmux=1`;
* опрашивает каждые 20 мс, антидребезг 2 отсчёта;
* отдаёт события через `sweet_hid_key(0/1/2)`.

Вызов стоит в `sweet_emmc_rootdev()` до инициализации eMMC, поэтому кнопки
заработают даже если root не смонтируется. Это удобно для диагностики.

### 4.2 Батарея

Патч 0097 добавляет `bsd/dev/a10_battery.c` и ветку в `SweetBattery`:

* I2C3 `0x13830000`, gauge `0x3b`: RVBAT `0x04`, RCUR_CC `0x06`, RSOC `0x08`;
* I2C4 `0x13840000`, charger `0x3d`: STATUS0 `0x08` (VBUS_OK, CHG_OK,
  CHG_STATUS);
* проценты — из RSOC, если он правдоподобен, иначе из напряжения;
* ток — знаковый, положительный = заряд;
* только чтение, никакой конфигурации PMIC.

Включается boot-аргументом `a10battery=1`. Без него `SweetBattery` остаётся
на прежнем fallback 100 % AC.

## 5. GPU: почему ускорения не будет

iOS-графический стек устроен так:

```
Metal / OpenGL ES -> userland driver -> AGX kernel driver -> Apple GPU
```

У A10 стоит **Mali-G71 MP2**. Это другой ISA, другая прошивка, другой
планировщик и другая модель памяти. Драйвер AGX физически не может управлять
Mali, а драйвера Mali для iOS userland не существует. Порт Panfrost/Mesa
сам по себе не даст Metal: нужен ещё слой трансляции Metal -> Gallium,
которого нет.

При этом текущий GUI sweet работает **без GPU-драйвера**: CoreAnimation
рисует на CPU, `backboardd` отдаёт IOSurface, `AppleCLCD` копирует его на
панель. Значит, GPU не является блокером запуска; блокер — CPU и память.

Реалистичные ускорения:

1. **SMP**: сейчас `cpus=1`; PSCI CPU_ON не подтверждается. Это даст больше,
   чем любой GPU-драйвер.
2. **Native 720x1600**: `sweetfb_native=1` уменьшает число пикселей в 2,4 раза.
3. **Zero-copy DECON**: заставить DECON сканировать IOSurface напрямую, без
   копии в boot framebuffer. Это 2D-ускорение презентации, не 3D.
4. **Снижение частоты презентации**: `sweetfb_hz=10` уже стоит по умолчанию.

Ожидаемая производительность без SMP: 1–5 кадров/с на простом UI, меньше 1
кадра/с на сложных экранах, задержка ввода десятки секунд. После SMP и
native-разрешения можно надеяться на 10–20 кадров/с в простых экранах, но
это всё равно не GPU-ускорение.

## 6. Прогноз по вероятностям

| Событие | Оценка |
|---|---|
| HFSX монтируется, `BSD root: emmcroot0` | 95 % |
| `launchd` запускается | 80 % |
| Появляются логи userland (runningboardd/backboardd) | 65 % |
| Процесс SpringBoard стартует | 50 % |
| Setup/Home Screen что-то рисует | 35 % |
| UI отзывается на кнопки | 20 % |
| UI отзывается на тач (без драйвера) | 0 % |
| 10+ кадров/с | 3 % |
| GPU-ускорение | 0 % |

Причина не в одном факторе, а в сумме: чистый образ без твиков, отсутствие
`/product`, 2 ГБ RAM, один CPU, отсутствие тача и медленный eMMC PIO.

## 7. Что делать по порядку

1. Дождаться артефакта, распаковать на microSD, записать `userdata`,
   прошить probe16, сфотографировать экран.
2. По фото понять точку остановки: HFSX, launchd, демон, паника.
3. Если дошли до userland — внести userland-твики sweet v14 (Setup,
   активация, CommCenter, watchdog, VirtualAudio, миграция). Это вероятнее
   всего понадобится до первого Home Screen.
4. Добавить `/product` из `DeviceTree.d22ap.im4p`.
5. Включить SMP (PSCI), затем кнопки/батарею проверить на устройстве.
6. Тач: достать `s6d7at0b01_a10.fw` из stock vendor и писать HSI2C/sec_ts.
7. GPU не трогать; при желании — zero-copy DECON и native resolution.

## 8. Итог

Первый запуск, скорее всего, дойдёт до `launchd` и упрётся в userland
(Setup/активация/миграция), а не в kernel. Это ожидаемо и полезно: мы
получим точку отказа. Рабочий GUI на A10 возможен только после SMP,
userland-твиков и тача; GPU-ускорение через Mali невозможно, но для iOS GUI
оно и не требуется — нужен быстрый CPU и дешёвая презентация кадра.

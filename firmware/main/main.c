// SureFlap Local Hub for ESP32-C5 (ESP-IDF v5.5+)
//
// Universal, NVS-configurable IEEE 802.15.4 / MiWi coordinator for SureFlap Microchip
// Pet Door / Cat Flap Connect on Channel 15 (PAN 0x3421).
//
// First boot (empty NVS, no compiled secrets.h):
//   Starts open Wi-Fi AP "SureFlap-Hub-Setup" -> http://192.168.4.1/ to configure
//   Wi-Fi SSID/password, bridge server IP, and country code (persisted in NVS across OTAs).
//
// Runtime UDP control (port 17755):
//   setlock <0..4|unlocked|keepin|keepout|locked|curfew> [op_hex] [retries]
//   writereg <reg_dec> <val_hex_or_dec> [op_hex] [retries]
//   txraw <hex_payload> [retries]
//   setkey <hdr4> <k4..k11_16> <chk12_2>
//   getkey | cancel | sink <ipv4> | wifi <ssid> <pass> | factoryreset
//   lock <ch> | hop [dwell_ms] | kb <i> <v> | ota http://<sink_ip>:<port>/path

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_ieee802154.h"
#include "esp_coexist.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "lwip/sockets.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#ifndef WIFI5_SSID
#define WIFI5_SSID ""
#endif
#ifndef WIFI5_PASS
#define WIFI5_PASS ""
#endif
#ifndef WIFI_SSID
#define WIFI_SSID WIFI5_SSID
#endif
#ifndef WIFI_PASS
#define WIFI_PASS WIFI5_PASS
#endif
#ifndef SINK_IP
#define SINK_IP ""
#endif
#ifndef WIFI_COUNTRY
#define WIFI_COUNTRY "GB"
#endif
#ifndef OTA_PORT
#define OTA_PORT 8000
#endif

#define ZEP_PORT   17754
#define CTRL_PORT  17755
#define STAT_PORT  17756
#define HUB_PAN    0x3421
#define NVS_NS     "sfhub"

static const char *TAG = "sureflap_hub";

typedef struct {
    char wifi5_ssid[33];
    char wifi5_pass[65];
    char wifi2_ssid[33];
    char wifi2_pass[65];
    char sink_ip[20];
    char country[4];
    uint16_t ota_port;
    uint8_t hub_mac[8];
} hub_cfg_t;

static hub_cfg_t g_cfg;

static void cfg_save(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, "w5s", g_cfg.wifi5_ssid);
    nvs_set_str(h, "w5p", g_cfg.wifi5_pass);
    nvs_set_str(h, "w2s", g_cfg.wifi2_ssid);
    nvs_set_str(h, "w2p", g_cfg.wifi2_pass);
    nvs_set_str(h, "sink", g_cfg.sink_ip);
    nvs_set_str(h, "cc", g_cfg.country);
    nvs_set_u16(h, "otap", g_cfg.ota_port);
    nvs_set_blob(h, "mac", g_cfg.hub_mac, 8);
    nvs_commit(h);
    nvs_close(h);
}

static void sanitize_cfg(void);

static void cfg_load(void)
{
    memset(&g_cfg, 0, sizeof g_cfg);
    strncpy(g_cfg.wifi5_ssid, WIFI5_SSID, sizeof(g_cfg.wifi5_ssid) - 1);
    strncpy(g_cfg.wifi5_pass, WIFI5_PASS, sizeof(g_cfg.wifi5_pass) - 1);
    strncpy(g_cfg.wifi2_ssid, WIFI_SSID, sizeof(g_cfg.wifi2_ssid) - 1);
    strncpy(g_cfg.wifi2_pass, WIFI_PASS, sizeof(g_cfg.wifi2_pass) - 1);
    strncpy(g_cfg.sink_ip, SINK_IP, sizeof(g_cfg.sink_ip) - 1);
    strncpy(g_cfg.country, WIFI_COUNTRY, sizeof(g_cfg.country) - 1);
    g_cfg.ota_port = OTA_PORT;

#ifdef HUB_EUI64_INIT
    uint8_t def_mac[8] = HUB_EUI64_INIT;
    memcpy(g_cfg.hub_mac, def_mac, 8);
#else
    uint8_t base_mac[6] = { 0 };
    esp_read_mac(base_mac, ESP_MAC_WIFI_STA);
    g_cfg.hub_mac[0] = 0x80; g_cfg.hub_mac[1] = 0x1f; g_cfg.hub_mac[2] = 0x12;
    g_cfg.hub_mac[3] = 0xff; g_cfg.hub_mac[4] = 0xfe;
    g_cfg.hub_mac[5] = base_mac[3]; g_cfg.hub_mac[6] = base_mac[4]; g_cfg.hub_mac[7] = base_mac[5];
#endif

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t l;
        l = sizeof g_cfg.wifi5_ssid; nvs_get_str(h, "w5s", g_cfg.wifi5_ssid, &l);
        l = sizeof g_cfg.wifi5_pass; nvs_get_str(h, "w5p", g_cfg.wifi5_pass, &l);
        l = sizeof g_cfg.wifi2_ssid; nvs_get_str(h, "w2s", g_cfg.wifi2_ssid, &l);
        l = sizeof g_cfg.wifi2_pass; nvs_get_str(h, "w2p", g_cfg.wifi2_pass, &l);
        l = sizeof g_cfg.sink_ip;    nvs_get_str(h, "sink", g_cfg.sink_ip, &l);
        l = sizeof g_cfg.country;    nvs_get_str(h, "cc", g_cfg.country, &l);
        uint16_t p = 0; if (nvs_get_u16(h, "otap", &p) == ESP_OK && p > 0) g_cfg.ota_port = p;
        l = 8; nvs_get_blob(h, "mac", g_cfg.hub_mac, &l);
        nvs_close(h);
    }
    sanitize_cfg();
}

static void sanitize_cfg(void)
{
    // Trim leading/trailing whitespace on sink_ip and country
    char *s = g_cfg.sink_ip;
    while (*s == ' ' || *s == '\t') s++;
    if (s != g_cfg.sink_ip) memmove(g_cfg.sink_ip, s, strlen(s) + 1);
    size_t n = strlen(g_cfg.sink_ip);
    while (n > 0 && (g_cfg.sink_ip[n - 1] == ' ' || g_cfg.sink_ip[n - 1] == '\t')) g_cfg.sink_ip[--n] = '\0';

    // Uppercase 2-letter ISO country code; default to GB if invalid
    if (isalpha((unsigned char)g_cfg.country[0]) && isalpha((unsigned char)g_cfg.country[1])) {
        g_cfg.country[0] = (char)toupper((unsigned char)g_cfg.country[0]);
        g_cfg.country[1] = (char)toupper((unsigned char)g_cfg.country[1]);
        g_cfg.country[2] = '\0';
        if (!strcmp(g_cfg.country, "UK")) strcpy(g_cfg.country, "GB");
    } else {
        strcpy(g_cfg.country, "GB");
    }
    if (!g_cfg.wifi2_ssid[0]) {
        strncpy(g_cfg.wifi2_ssid, g_cfg.wifi5_ssid, sizeof(g_cfg.wifi2_ssid) - 1);
        strncpy(g_cfg.wifi2_pass, g_cfg.wifi5_pass, sizeof(g_cfg.wifi2_pass) - 1);
    }
    if (!g_cfg.ota_port) g_cfg.ota_port = 8000;
}

static bool cfg_is_ready(void)
{
    struct in_addr tmp;
    return g_cfg.wifi5_ssid[0] != '\0' && inet_pton(AF_INET, g_cfg.sink_ip, &tmp) == 1;
}

static void url_decode(const char *src, char *dst, size_t max_len)
{
    size_t i = 0;
    while (*src && i + 1 < max_len) {
        if (*src == '+') { dst[i++] = ' '; src++; }
        else if (*src == '%' && isxdigit((unsigned char)src[1]) && isxdigit((unsigned char)src[2])) {
            int hi = isdigit((unsigned char)src[1]) ? src[1] - '0' : tolower((unsigned char)src[1]) - 'a' + 10;
            int lo = isdigit((unsigned char)src[2]) ? src[2] - '0' : tolower((unsigned char)src[2]) - 'a' + 10;
            dst[i++] = (char)((hi << 4) | lo);
            src += 3;
        } else { dst[i++] = *src++; }
    }
    dst[i] = '\0';
}

static bool form_get(const char *body, const char *key, char *out, size_t max_len)
{
    size_t klen = strlen(key);
    const char *p = body;
    while (p && *p) {
        if (!strncmp(p, key, klen) && p[klen] == '=') {
            const char *v = p + klen + 1;
            const char *amp = strchr(v, '&');
            size_t vlen = amp ? (size_t)(amp - v) : strlen(v);
            char tmp[192];
            if (vlen >= sizeof tmp) vlen = sizeof(tmp) - 1;
            memcpy(tmp, v, vlen); tmp[vlen] = '\0';
            url_decode(tmp, out, max_len);
            return true;
        }
        p = strchr(p, '&');
        if (p) p++;
    }
    return false;
}

static void run_setup_portal(int timeout_sec)
{
    ESP_LOGW(TAG, "Starting SoftAP 'SureFlap-Hub-Setup' on http://192.168.4.1/ (timeout=%ds)", timeout_sec);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wcfg));
    wifi_config_t ap = { 0 };
    strcpy((char *)ap.ap.ssid, "SureFlap-Hub-Setup");
    ap.ap.ssid_len = strlen("SureFlap-Hub-Setup");
    ap.ap.channel = 6;
    ap.ap.max_connection = 4;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof opt);
    struct timeval stv = { .tv_sec = 5 };
    setsockopt(srv, SOL_SOCKET, SO_RCVTIMEO, &stv, sizeof stv);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(80), .sin_addr.s_addr = htonl(INADDR_ANY) };
    bind(srv, (struct sockaddr *)&a, sizeof a);
    listen(srv, 4);

    static const char *HTML_PAGE =
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n"
        "<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SureFlap ESP32-C5 Setup</title><style>"
        "body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;max-width:420px;margin:24px auto;padding:16px}"
        "input,button{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 14px;border-radius:8px;border:1px solid #30363d;background:#161b22;color:#e6edf3;font-size:15px}"
        "button{background:#238636;border:none;font-weight:600;cursor:pointer}"
        "label{font-size:13px;color:#9198a1}h2{margin-top:0}"
        "</style></head><body><h2>SureFlap ESP32-C5 Hub Setup</h2>"
        "<form method=POST action=/save>"
        "<label>Primary Wi-Fi SSID (5 GHz recommended)</label><input name=w5s required>"
        "<label>Primary Wi-Fi Password</label><input name=w5p type=password>"
        "<label>2.4 GHz Fallback SSID (optional)</label><input name=w2s>"
        "<label>2.4 GHz Fallback Password (optional)</label><input name=w2p type=password>"
        "<label>Bridge / Docker Server IPv4 (e.g. 192.168.1.50)</label><input name=sink required>"
        "<label>Wi-Fi Country Code (2-letter ISO, e.g. GB, US, DE)</label><input name=cc value=GB maxlength=2>"
        "<button type=submit>Save &amp; Reboot</button></form></body></html>";

    static char req[2560];
    int64_t deadline = timeout_sec > 0 ? esp_timer_get_time() + (int64_t)timeout_sec * 1000000LL : 0;
    for (;;) {
        if (deadline > 0 && esp_timer_get_time() >= deadline) {
            ESP_LOGW(TAG, "Setup portal fallback timeout reached — rebooting to retry Wi-Fi");
            esp_restart();
        }
        int c = accept(srv, NULL, NULL);
        if (c < 0) continue;
        struct timeval tv = { .tv_sec = 3 };
        setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

        // Read full HTTP headers + POST body across multiple TCP segments
        int total_n = 0;
        char *body = NULL;
        int content_len = 0;
        while (total_n < (int)sizeof(req) - 1) {
            int n = recv(c, req + total_n, sizeof(req) - 1 - total_n, 0);
            if (n <= 0) break;
            total_n += n;
            req[total_n] = '\0';
            if (!body) {
                body = strstr(req, "\r\n\r\n");
                if (body) {
                    body += 4;
                    const char *cl = req;
                    while (cl < body) {
                        if (!strncasecmp(cl, "Content-Length:", 15)) {
                            content_len = atoi(cl + 15);
                            break;
                        }
                        cl = strchr(cl, '\n');
                        if (!cl) break;
                        cl++;
                    }
                }
            }
            if (body && (total_n - (int)(body - req)) >= content_len) break;
        }

        if (total_n > 0) {
            if (!strncmp(req, "POST /save", 10) && body) {
                form_get(body, "w5s", g_cfg.wifi5_ssid, sizeof g_cfg.wifi5_ssid);
                form_get(body, "w5p", g_cfg.wifi5_pass, sizeof g_cfg.wifi5_pass);
                form_get(body, "w2s", g_cfg.wifi2_ssid, sizeof g_cfg.wifi2_ssid);
                form_get(body, "w2p", g_cfg.wifi2_pass, sizeof g_cfg.wifi2_pass);
                form_get(body, "sink", g_cfg.sink_ip, sizeof g_cfg.sink_ip);
                form_get(body, "cc", g_cfg.country, sizeof g_cfg.country);
                sanitize_cfg();
                if (cfg_is_ready()) {
                    cfg_save();
                    const char *ok = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
                                     "<html><body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:24px'>"
                                     "<h2>Saved!</h2><p>Rebooting into SureFlap Hub mode...</p></body></html>";
                    send(c, ok, strlen(ok), 0);
                    close(c);
                    vTaskDelay(pdMS_TO_TICKS(1000));
                    esp_restart();
                } else {
                    const char *err = "HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
                                      "<html><body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:24px'>"
                                      "<h2>Invalid Settings</h2><p>Please enter a Wi-Fi SSID and a valid IPv4 address (e.g. 192.168.1.50).</p>"
                                      "<p><a href='/' style='color:#58a6ff'>Go back</a></p></body></html>";
                    send(c, err, strlen(err), 0);
                    close(c);
                    continue;
                }
            }
            send(c, HTML_PAGE, strlen(HTML_PAGE), 0);
        }
        close(c);
    }
}

typedef struct {
    uint8_t len;
    uint8_t ch;
    int8_t rssi;
    uint64_t ts;
    uint8_t d[127];
} rxf_t;

static QueueHandle_t q;
static volatile uint32_t dropped, total;
static volatile uint32_t per_ch[27];
static volatile bool hopping = false;
static volatile uint32_t dwell_ms = 4000;
static volatile uint8_t cur_ch = 15;
static volatile bool wifi_up = false;
static int fails;
static RTC_NOINIT_ATTR volatile uint8_t fail5, fail5_magic;
static bool use5g = true;
static RTC_NOINIT_ATTR volatile uint8_t crumb[4];
static uint8_t prev_crumb[4];
static RTC_NOINIT_ATTR volatile uint8_t disc_last, disc_cnt;
static uint8_t prev_disc[2];
static uint32_t zseq;

static uint8_t hub_mac[8] = { 0x80, 0x1f, 0x12, 0xff, 0xfe, 0x00, 0x01, 0x02 };
static volatile bool r154_started, radio_paused;
static void hub_log_str(const char *ev, const char *t);

// Runtime tuning knobs kb[0..11]:
//   0: beacon reply (0=auto, 1=0x0a, 2=0x09)
//   1: data reply (0=0x02, 1=0x09, 2=auto)
//   2: echo dev_ctr
//   3: request MAC ACK on data-reply
//   4: start-reply type (0=0x14, 1=0x02)
//   5: mute TX (1=listen-only)
//   6: rx window mode (0=always-on, 1=sleep, 2=timed 5.56 s sync window)
//   7: wifi power-save override
//   8: reserved
//   9: command TX delay after 0x09 in ms (0 => default 15 ms)
//  10: disable MAC ACK request on outbound command TX
static volatile int kb[12] = { 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, 0, 0 };
static volatile bool dev_online;
static volatile int radio_cmd; // 1 = sleep, 2 = wake
static volatile int64_t last_beacon_us;
static volatile int64_t hold_awake_until_us;
static volatile int wake_err, sleep_err, wakes, kbps_applied = -1;

static volatile uint32_t tx_ok, tx_acked, tx_fail, tx_err;
static volatile int8_t last_rssi;
static uint8_t hub_seq, hub_txseq, dev_ctr;
static int stat_sock = -1;
static struct sockaddr_in stat_to;

// Per-session XOR keystream for SureFlap Pet Door Connect register frames.
// Auto-learned from the first 24B movement frame and 12B register frame after pairing,
// or pushed explicitly via UDP "setkey <hdr4> <k4..k11_16> <chk12_2>".
static portMUX_TYPE cmd_mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t sess_hdr[2] = { 0x00, 0x00 };
static uint8_t sess_k[12]  = { 0 };
static uint8_t sess_chk12  = 0x00;
static bool sess_valid = false;

// Outbound command queue (delivered on the flap's next 0x08 check-in after 0x09 stay-awake)
static uint8_t cmd_buf[64];
static int cmd_len = 0;
static int cmd_retries = 0;
static bool cmd_auto_seq = false;
static bool cmd_waiting_ack = false;

static void log_scan(void)
{
    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > 20) n = 20;
    static wifi_ap_record_t r[20];
    esp_wifi_scan_get_ap_records(&n, r);
    ESP_LOGW(TAG, "scan: %d APs", n);
    for (int i = 0; i < n; i++) {
        ESP_LOGW(TAG, "  ap '%s' ch=%d rssi=%d auth=%d", r[i].ssid, r[i].primary, r[i].rssi, r[i].authmode);
    }
}

void IRAM_ATTR esp_ieee802154_receive_done(uint8_t *frame, esp_ieee802154_frame_info_t *info)
{
    rxf_t f;
    f.len = frame[0] > 127 ? 127 : frame[0];
    f.ch = info->channel;
    f.rssi = info->rssi;
    f.ts = info->timestamp;
    memcpy(f.d, frame + 1, f.len);
    esp_ieee802154_receive_handle_done(frame);
    BaseType_t hp = pdFALSE;
    if (xQueueSendFromISR(q, &f, &hp) != pdTRUE) dropped++;
    total++;
    if (f.ch < 27) per_ch[f.ch]++;
    if (hp) portYIELD_FROM_ISR();
}

void IRAM_ATTR esp_ieee802154_transmit_done(const uint8_t *frame, const uint8_t *ack, esp_ieee802154_frame_info_t *ack_info)
{
    tx_ok++;
    if (ack) {
        tx_acked++;
        esp_ieee802154_receive_handle_done((uint8_t *)ack);
    }
}

void IRAM_ATTR esp_ieee802154_transmit_failed(const uint8_t *frame, esp_ieee802154_tx_error_t error)
{
    tx_fail++;
    tx_err = error;
}

static void set_ch(uint8_t ch)
{
    cur_ch = ch;
    esp_ieee802154_set_channel(ch);
    esp_ieee802154_receive();
}

static void hop_task(void *a)
{
    uint8_t ch = cur_ch;
    set_ch(ch);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(hopping ? dwell_ms : 500));
        if (hopping) {
            ch = (cur_ch >= 26) ? 11 : cur_ch + 1;
            set_ch(ch);
        }
    }
}

static void radio_task(void *a)
{
    int64_t next_on = 0, off_at = 0, seen = 0, lastlog = 0;
    bool awake = true, synced = false, hit = false;
    int misses = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        int c = radio_cmd;
        radio_cmd = 0;
        if (kb[7] != kbps_applied) {
            kbps_applied = kb[7];
            esp_wifi_set_ps(kb[7] ? WIFI_PS_MIN_MODEM : (use5g ? WIFI_PS_NONE : WIFI_PS_MIN_MODEM));
        }
        if (!r154_started) continue;
        if (kb[6] == 1) {
            if (!radio_paused) { esp_ieee802154_sleep(); radio_paused = true; }
            continue;
        }
        if (kb[6] == 2) {
            int64_t now = esp_timer_get_time();
            if (last_beacon_us != seen) {
                seen = last_beacon_us;
                synced = true;
                hit = true;
                misses = 0;
                next_on = seen + 5560000 - 250000;
                off_at = seen + 700000;
            }
            if (hold_awake_until_us > off_at) off_at = hold_awake_until_us;
            if (awake && now > off_at) {
                if (esp_ieee802154_sleep() != ESP_OK) sleep_err++;
                awake = false;
                radio_paused = true;
                if (!hit && ++misses >= 2) synced = false;
                hit = false;
                if (synced) {
                    while (next_on <= now) next_on += 5560000;
                } else {
                    next_on = now + 600000;
                }
            } else if (!awake && now >= next_on) {
                esp_ieee802154_set_channel(cur_ch);
                if (esp_ieee802154_receive() != ESP_OK) wake_err++;
                wakes++;
                awake = true;
                radio_paused = false;
                hit = false;
                off_at = now + (synced ? 700000 : 1500000);
            }
            if (now - lastlog > 15000000) {
                lastlog = now;
                char t[80];
                snprintf(t, sizeof t, "wakes=%d wake_err=%d sleep_err=%d synced=%d awake=%d",
                         wakes, wake_err, sleep_err, synced, awake);
                hub_log_str("rx2 ", t);
            }
            continue;
        }
        if (c == 1 && !radio_paused) { esp_ieee802154_sleep(); radio_paused = true; }
        else if (c == 2 && radio_paused) { esp_ieee802154_receive(); radio_paused = false; }
    }
}

static void start_154(void)
{
    if (r154_started) return;
    r154_started = true;
    ESP_ERROR_CHECK(esp_ieee802154_enable());
    ESP_ERROR_CHECK(esp_ieee802154_set_promiscuous(false));
    ESP_ERROR_CHECK(esp_ieee802154_set_panid(HUB_PAN));
    ESP_ERROR_CHECK(esp_ieee802154_set_extended_address(hub_mac));
    ESP_ERROR_CHECK(esp_ieee802154_set_coordinator(true));
    ESP_ERROR_CHECK(esp_ieee802154_set_short_address(0xfffe));
    ESP_ERROR_CHECK(esp_ieee802154_set_rx_when_idle(true));
    xTaskCreate(hop_task, "hop", 4096, NULL, 5, NULL);
    if (!use5g) esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
    ESP_LOGW(TAG, "802.15.4 started on ch %u PAN 0x%04x", cur_ch, HUB_PAN);
}

static void wifi_ev(void *a, esp_event_base_t b, int32_t id, void *d)
{
    if (b == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_set_band_mode(use5g ? WIFI_BAND_MODE_5G_ONLY : WIFI_BAND_MODE_2G_ONLY);
        esp_wifi_connect();
    } else if (b == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *e = d;
        wifi_up = false;
        if (r154_started && !use5g) radio_cmd = 1;
        disc_last = e->reason;
        if (disc_cnt < 255) disc_cnt++;
        ESP_LOGW(TAG, "wifi down reason=%d fails=%d", e->reason, fails);
        ++fails;
        if (fails % 3 == 1) esp_wifi_scan_start(NULL, false);
        else esp_wifi_connect();
    } else if (b == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        log_scan();
        esp_wifi_connect();
    } else if (b == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        wifi_up = true;
        ip_event_got_ip_t *e = d;
        ESP_LOGW(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
    }
}

static void wifi_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    if (esp_wifi_set_country_code(g_cfg.country, true) != ESP_OK) {
        ESP_LOGW(TAG, "Invalid Wi-Fi country '%s', falling back to GB", g_cfg.country);
        esp_wifi_set_country_code("GB", true);
    }
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_ev, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_ev, NULL));
    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, use5g ? g_cfg.wifi5_ssid : g_cfg.wifi2_ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, use5g ? g_cfg.wifi5_pass : g_cfg.wifi2_pass, sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = wc.sta.password[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    wc.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wc.sta.pmf_cfg.capable = true;
    wc.sta.pmf_cfg.required = false;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static void hub_log(const char *ev, const uint8_t *p, int n)
{
    if (stat_sock < 0 || !wifi_up) return;
    char b[300];
    int k = snprintf(b, sizeof b, "HUB rssi=%d tx[ok=%u acked=%u fail=%u err=%u] %s",
                     last_rssi, (unsigned)tx_ok, (unsigned)tx_acked, (unsigned)tx_fail, (unsigned)tx_err, ev);
    for (int i = 0; i < n && k < (int)sizeof b - 3; i++) k += snprintf(b + k, sizeof b - k, "%02x", p[i]);
    sendto(stat_sock, b, k, MSG_DONTWAIT, (struct sockaddr *)&stat_to, sizeof(stat_to));
    sendto(stat_sock, b, k, MSG_DONTWAIT, (struct sockaddr *)&stat_to, sizeof(stat_to));
}

static void hub_log_str(const char *ev, const char *t)
{
    if (stat_sock < 0 || !wifi_up) return;
    char b[180];
    int k = snprintf(b, sizeof b, "HUB %s%s", ev, t);
    sendto(stat_sock, b, k, MSG_DONTWAIT, (struct sockaddr *)&stat_to, sizeof(stat_to));
}

static void log_key_state(void)
{
    char t[128];
    taskENTER_CRITICAL(&cmd_mux);
    snprintf(t, sizeof t, "hdr=%02x%02x k4..11=%02x%02x%02x%02x%02x%02x%02x%02x chk12=%02x valid=%d pend=%d",
             sess_hdr[0], sess_hdr[1],
             sess_k[4], sess_k[5], sess_k[6], sess_k[7], sess_k[8], sess_k[9], sess_k[10], sess_k[11],
             sess_chk12, sess_valid ? 1 : 0, cmd_retries);
    taskEXIT_CRITICAL(&cmd_mux);
    hub_log_str("key ", t);
}

static void learn_session_from_rx(const uint8_t *p, int n)
{
    if (n < 12 || p[0] != 0x01) return;
    bool changed = false;
    taskENTER_CRITICAL(&cmd_mux);
    if (p[2] != sess_hdr[0] || p[3] != sess_hdr[1]) {
        sess_hdr[0] = p[2];
        sess_hdr[1] = p[3];
        sess_k[4] = p[4] ^ (uint8_t)(n - 2);
        sess_k[5] = p[5] ^ 0x84;
        sess_k[8] = p[8];
        sess_valid = false;
        changed = true;
    }
    if (n == 24) {
        // 24B Pet Door movement frame is always reg 1024 (0x0400), rlen = 13
        uint8_t k6 = p[6] ^ 0x04, k7 = p[7] ^ 0x00, k9 = p[9] ^ 13;
        if (sess_k[6] != k6 || sess_k[7] != k7 || sess_k[9] != k9) {
            sess_k[6] = k6; sess_k[7] = k7; sess_k[9] = k9;
            changed = true;
        }
    } else if (n == 12) {
        uint8_t x = 0;
        for (int i = 4; i < 12; i++) x ^= p[i];
        if (sess_chk12 != x) { sess_chk12 = x; changed = true; }
    }
    taskEXIT_CRITICAL(&cmd_mux);
    if (changed) log_key_state();
}

static int queue_writereg1(uint16_t reg, uint8_t val, uint8_t op, int retries)
{
    uint8_t pkt[12];
    taskENTER_CRITICAL(&cmd_mux);
    if (!sess_valid) {
        taskEXIT_CRITICAL(&cmd_mux);
        hub_log_str("cmd-err ", "session key not verified/set yet (waiting for bridge setkey)");
        return -1;
    }
    pkt[0]  = 0x01;
    pkt[1]  = 0x00;                       // stamped with hub_txseq++ at TX time
    pkt[2]  = sess_hdr[0];
    pkt[3]  = sess_hdr[1];
    pkt[4]  = 10 ^ sess_k[4];             // len - 2 = 10
    pkt[5]  = op ^ sess_k[5];             // opcode (0x02 = Write Register)
    pkt[6]  = (uint8_t)(reg >> 8) ^ sess_k[6];
    pkt[7]  = (uint8_t)(reg & 0xff) ^ sess_k[7];
    pkt[8]  = 0x00 ^ sess_k[8];
    pkt[9]  = 0x01 ^ sess_k[9];           // rlen = 1
    pkt[10] = val ^ sess_k[10];
    uint8_t x = sess_chk12;
    for (int i = 4; i <= 10; i++) x ^= pkt[i];
    pkt[11] = x;                          // preserves XOR(pkt[4..11]) == sess_chk12
    memcpy(cmd_buf, pkt, 12);
    cmd_len = 12;
    cmd_retries = retries > 0 ? retries : 3;
    cmd_auto_seq = true;
    cmd_waiting_ack = false;
    taskEXIT_CRITICAL(&cmd_mux);
    hub_log("cmd-queued ", pkt, 12);
    return 0;
}

static int parse_hex_bytes(const char *s, uint8_t *out, int max_n)
{
    int n = 0;
    while (*s && n < max_n) {
        while (*s == ' ' || *s == ':' || *s == '-') s++;
        if (!isxdigit((unsigned char)s[0]) || !isxdigit((unsigned char)s[1])) break;
        int hi = isdigit((unsigned char)s[0]) ? s[0] - '0' : tolower((unsigned char)s[0]) - 'a' + 10;
        int lo = isdigit((unsigned char)s[1]) ? s[1] - '0' : tolower((unsigned char)s[1]) - 'a' + 10;
        out[n++] = (uint8_t)((hi << 4) | lo);
        s += 2;
    }
    return n;
}

static void hub_tx(uint8_t type, bool ack, const uint8_t *dst, const uint8_t *pl, int n)
{
    if (kb[5]) return;
    static uint8_t txb[4][128];
    static unsigned txi;
    uint8_t *f = txb[txi++ & 3];
    int i = 1;
    uint16_t fcf = type | (1 << 14) | (1 << 15);
    if (dst) fcf |= (1 << 6) | (ack ? 1 << 5 : 0) | (3 << 10);
    f[i++] = fcf & 0xff;
    f[i++] = fcf >> 8;
    f[i++] = hub_seq++;
    f[i++] = HUB_PAN & 0xff;
    f[i++] = HUB_PAN >> 8;
    if (dst) { memcpy(f + i, dst, 8); i += 8; }
    memcpy(f + i, hub_mac, 8);
    i += 8;
    memcpy(f + i, pl, n);
    i += n;
    f[0] = i - 1 + 2; // PHR includes 2-byte hardware FCS
    crumb[0] = 20;
    esp_ieee802154_transmit(f, false);
    crumb[0] = 21;
}

static void hub_handle(const rxf_t *r)
{
    last_rssi = r->rssi;
    const uint8_t *d = r->d;
    int len = r->len - 2; // strip FCS
    if (len < 4) return;
    uint16_t fcf = d[0] | (d[1] << 8);
    int dam = (fcf >> 10) & 3, sam = (fcf >> 14) & 3, panc = (fcf >> 6) & 1;
    int o = 3;
    if (dam) o += 2 + (dam == 2 ? 2 : 8);
    if (!sam) return;
    if (!panc) o += 2;
    if (sam != 3 || o + 8 > len) return;
    const uint8_t *src = d + o;
    o += 8;
    const uint8_t *p = d + o;
    int n = len - o;
    if (n < 1) return;

    uint8_t out[8];
    if (p[0] == 0x07) { // Beacon Request -> Coordinator Beacon
        dev_online = false;
        uint8_t b[7] = { 0xff, 0x45, 0x00, 0x00, 0x7e, 0x02, 0x00 };
        if (n > 1 && p[1] == 0x0f) b[5] = 0x01;
        else if (n > 1 && p[1] == 0x2f) b[5] = 0x02;
        hub_tx(0, false, NULL, b, 7);
        hub_log("beaconreq ", p, n);
    } else if (p[0] == 0x01 && !dev_online && (fcf & 7) == 3) { // Association Request
        uint8_t a[4] = { 0x02, 0xfe, 0xff, 0x00 };
        hub_tx(3, true, src, a, 4);
        hub_log("assoc ", p, n);
    } else if (p[0] == 0x13 && n > 1) { // Session Start -> Online
        dev_online = true;
        dev_ctr = p[1];
        out[0] = kb[4] ? 0x02 : 0x14;
        out[1] = hub_txseq++;
        out[2] = p[1];
        out[3] = 0;
        hub_tx(1, true, src, out, 4);
        hub_log("start ", p, n);
    } else if (p[0] == 0x08 && n > 2) { // 5.56 s Periodic Online Beacon
        last_beacon_us = esp_timer_get_time();
        dev_online = true;
        uint8_t txpkt[64];
        int txlen = 0;
        bool auto_seq = false;
        taskENTER_CRITICAL(&cmd_mux);
        if (cmd_retries > 0 && cmd_len > 0) {
            txlen = cmd_len;
            memcpy(txpkt, cmd_buf, txlen);
            auto_seq = cmd_auto_seq;
            cmd_retries--;
            cmd_waiting_ack = true;
        }
        taskEXIT_CRITICAL(&cmd_mux);

        if (txlen > 0) {
            // Tell flap to stay awake (0x09), wait inside its ~200 ms RX window, then transmit
            out[0] = 0x09;
            out[1] = hub_txseq++;
            out[2] = dev_ctr;
            hub_tx(1, false, src, out, 3);
            hold_awake_until_us = esp_timer_get_time() + 1800000;
            hub_log("online ", p, n);
            int delay_ms = kb[9] > 0 ? kb[9] : 15;
            if (delay_ms > 150) delay_ms = 150;
            vTaskDelay(pdMS_TO_TICKS(delay_ms));
            if (auto_seq && txlen >= 2) txpkt[1] = hub_txseq++;
            hub_tx(1, kb[10] == 0, src, txpkt, txlen);
            hub_log("txcmd ", txpkt, txlen);
        } else {
            out[0] = kb[0] == 1 ? 0x0a : kb[0] == 2 ? 0x09 : ((p[2] & 1) ? 0x09 : 0x0a);
            out[1] = hub_txseq++;
            out[2] = dev_ctr;
            hub_tx(1, false, src, out, 3);
            hub_log("online ", p, n);
        }
    } else if (p[0] == 0x01 && dev_online && n > 2) { // Flap Data Frame
        dev_ctr = p[1];
        out[0] = kb[1] == 1 ? 0x09 : kb[1] == 2 ? ((p[2] & 1) ? 0x09 : 0x02) : 0x02;
        out[1] = hub_txseq++;
        out[2] = kb[2] ? dev_ctr : p[1];
        out[3] = 0;
        hub_tx(1, kb[3] != 0, src, out, 4);
        hub_log("data ", p, n);
        learn_session_from_rx(p, n);
    } else if (p[0] == 0x02 && n > 2) { // Flap MiWi ACK
        dev_online = true;
        bool was_waiting = false;
        taskENTER_CRITICAL(&cmd_mux);
        if (cmd_waiting_ack) {
            was_waiting = true;
            cmd_waiting_ack = false;
            cmd_retries = 0;
        }
        taskEXIT_CRITICAL(&cmd_mux);
        out[0] = was_waiting ? 0x0a : ((p[2] & 1) ? 0x09 : 0x0a);
        out[1] = hub_txseq++;
        out[2] = dev_ctr;
        out[3] = 0;
        hub_tx(1, false, src, out, 4);
        hub_log(was_waiting ? "txcmd-ack " : "ack ", p, n);
    } else if (p[0] == 0x0a && n > 1) {
        dev_ctr = p[1];
        hub_log("resync ", p, n);
    } else {
        hub_log("unknown ", p, n);
    }
}

static struct sockaddr_in zep_to;

static void update_sink_addrs(void)
{
    zep_to.sin_family = AF_INET;
    zep_to.sin_port = htons(ZEP_PORT);
    inet_pton(AF_INET, g_cfg.sink_ip, &zep_to.sin_addr);
    stat_to = zep_to;
    stat_to.sin_port = htons(STAT_PORT);
}

static void send_task(void *a)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    update_sink_addrs();
    stat_sock = s;

    rxf_t f;
    uint8_t pkt[32 + 127];
    for (;;) {
        crumb[0] = 1;
        if (xQueueReceive(q, &f, portMAX_DELAY) != pdTRUE) continue;
        crumb[0] = 2;
        if (f.ch == cur_ch) hub_handle(&f);
        crumb[0] = 3;
        if (!wifi_up) continue;

        memcpy(pkt, "EX", 2);
        pkt[2] = 2;
        pkt[3] = 1;
        pkt[4] = f.ch;
        pkt[5] = 0;
        pkt[6] = 1;
        pkt[7] = 1;
        pkt[8] = 0xff;
        uint64_t us = f.ts;
        uint32_t sec = us / 1000000 + 0x83AA7E80u;
        uint32_t frac = (uint32_t)(((us % 1000000) << 32) / 1000000);
        pkt[9] = sec >> 24; pkt[10] = sec >> 16; pkt[11] = sec >> 8; pkt[12] = sec;
        pkt[13] = frac >> 24; pkt[14] = frac >> 16; pkt[15] = frac >> 8; pkt[16] = frac;
        zseq++;
        pkt[17] = zseq >> 24; pkt[18] = zseq >> 16; pkt[19] = zseq >> 8; pkt[20] = zseq;
        memset(pkt + 21, 0, 10);
        pkt[28] = (uint8_t)f.rssi;
        pkt[31] = f.len;
        memcpy(pkt + 32, f.d, f.len);
        crumb[0] = 4;
        sendto(s, pkt, 32 + f.len, MSG_DONTWAIT, (struct sockaddr *)&zep_to, sizeof(zep_to));
        crumb[0] = 5;
    }
}

static void stat_msg(int s, struct sockaddr_in *to)
{
    char b[400];
    int n = snprintf(b, sizeof b, "STAT up=%d hop=%d dwell=%u cur=%u total=%u dropped=%u |",
                     wifi_up, hopping, (unsigned)dwell_ms, cur_ch, (unsigned)total, (unsigned)dropped);
    for (int c = 11; c <= 26; c++) n += snprintf(b + n, sizeof b - n, " %d:%u", c, (unsigned)per_ch[c]);
    sendto(s, b, n, 0, (struct sockaddr *)to, sizeof *to);
}

static volatile uint32_t hb;

static int http_get(const char *host, int port, const char *path, int (*body_cb)(const char *, int, void *), void *ctx)
{
    int s = socket(AF_INET, SOCK_STREAM, 0), got = -1;
    struct sockaddr_in to = { .sin_family = AF_INET, .sin_port = htons(port) };
    inet_pton(AF_INET, host, &to.sin_addr);
    struct timeval tv = { .tv_sec = 15 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    if (connect(s, (struct sockaddr *)&to, sizeof to) == 0) {
        char req[160];
        int n = snprintf(req, sizeof req, "GET %s HTTP/1.0\r\nHost: %s\r\n\r\n", path, host);
        send(s, req, n, 0);
        static char buf[1460];
        int st = 0, ok2 = 0, pos = 0;
        got = 0;
        char sig[10];
        while ((n = recv(s, buf, sizeof buf, 0)) > 0) {
            int i = 0;
            for (; st < 4 && i < n; i++) {
                if (pos < 10) sig[pos] = buf[i];
                pos++;
                char want = (st & 1) ? '\n' : '\r';
                st = (buf[i] == want) ? st + 1 : (buf[i] == '\r' ? 1 : 0);
            }
            if (st < 4) continue;
            if (!ok2) {
                if (pos < 10 || strncmp(sig, "HTTP/1.", 7) || sig[9] != '2') { got = -1; break; }
                ok2 = 1;
            }
            if (n - i > 0 && body_cb(buf + i, n - i, ctx) != 0) { got = -1; break; }
            got += n - i;
        }
    }
    close(s);
    return got;
}

static int ota_write_cb(const char *p, int n, void *ctx)
{
    return esp_ota_write(*(esp_ota_handle_t *)ctx, p, n) == ESP_OK ? 0 : -1;
}

static int ver_cb(const char *p, int n, void *ctx)
{
    char *v = ctx;
    int l = strlen(v);
    if (l + n > 31) n = 31 - l;
    memcpy(v + l, p, n);
    v[l + n] = 0;
    return 0;
}

static void ota_pull(const char *host, int port, const char *path, bool skip_if_same_ver)
{
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t h = 0;
    if (!part || esp_ota_begin(part, OTA_SIZE_UNKNOWN, &h) != ESP_OK) return;
    int got = http_get(host, port, path, ota_write_cb, &h);
    ESP_LOGW(TAG, "OTA got %d bytes", got);
    if (got > 100000 && esp_ota_end(h) == ESP_OK) {
        esp_app_desc_t new_desc;
        if (skip_if_same_ver && esp_ota_get_partition_description(part, &new_desc) == ESP_OK &&
            strcmp(new_desc.version, esp_app_get_description()->version) == 0) {
            ESP_LOGW(TAG, "OTA image version '%s' matches running app — skipping reboot to prevent loop", new_desc.version);
            return;
        }
        if (esp_ota_set_boot_partition(part) == ESP_OK) {
            ESP_LOGW(TAG, "OTA ok, restarting");
            esp_restart();
        }
        return;
    }
    esp_ota_abort(h);
}

static void ota_task(void *a)
{
    char *url = a, host[16], path[64];
    int port = 0;
    if (sscanf(url, "http://%15[^:]:%d%63s", host, &port, path) == 3) ota_pull(host, port, path, false);
    free(url);
    vTaskDelete(NULL);
}

static void boot_ota_check(void)
{
    char v[32] = "";
    int n = http_get(g_cfg.sink_ip, g_cfg.ota_port, "/ver.txt", ver_cb, v);
    while (n > 0 && strlen(v) > 0 && (v[strlen(v) - 1] == '\n' || v[strlen(v) - 1] == '\r' || v[strlen(v) - 1] == ' ')) {
        v[strlen(v) - 1] = 0;
    }
    const char *mine = esp_app_get_description()->version;
    hub_log_str(n > 0 ? (strcmp(v, mine) ? "ota-newer " : "ota-current ") : "ota-nocontact ", mine);
    if (n > 0 && strcmp(v, mine)) ota_pull(g_cfg.sink_ip, g_cfg.ota_port, "/sureflap_hub.bin", true);
}

static const char *parse_token(const char *p, char *out, size_t max_len)
{
    while (*p == ' ' || *p == '\t') p++;
    if (!*p) { out[0] = '\0'; return NULL; }
    size_t i = 0;
    if (*p == '"') {
        p++;
        while (*p && *p != '"') {
            if (*p == '\\' && p[1]) p++;
            if (i + 1 < max_len) out[i++] = *p;
            p++;
        }
        if (*p == '"') p++;
    } else {
        while (*p && *p != ' ' && *p != '\t') {
            if (i + 1 < max_len) out[i++] = *p;
            p++;
        }
    }
    out[i] = '\0';
    return p;
}

static void ctrl_task(void *a)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(CTRL_PORT), .sin_addr.s_addr = htonl(INADDR_ANY) };
    bind(s, (struct sockaddr *)&me, sizeof me);
    struct timeval tv = { .tv_sec = 10 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

    char b[200];
    for (;;) {
        crumb[1] = 1;
        int n = recvfrom(s, b, sizeof b - 1, 0, NULL, NULL);
        crumb[1] = 2;
        if (n > 0) {
            b[n] = 0;
            while (n > 0 && (b[n - 1] == '\n' || b[n - 1] == '\r' || b[n - 1] == ' ')) b[--n] = 0;
            unsigned v;
            if (sscanf(b, "lock %u", &v) == 1 && v >= 11 && v <= 26) {
                hopping = false;
                set_ch(v);
            } else if (sscanf(b, "hop %u", &v) == 1) {
                dwell_ms = v < 200 ? 200 : v;
                hopping = true;
            } else if (!strncmp(b, "kb ", 3)) {
                int i = 0, x = 0;
                if (sscanf(b + 3, "%d %d", &i, &x) == 2 && i >= 0 && i < 12) {
                    kb[i] = x;
                    char t[64];
                    snprintf(t, sizeof t, "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                             kb[0], kb[1], kb[2], kb[3], kb[4], kb[5], kb[6], kb[7], kb[8], kb[9], kb[10]);
                    hub_log_str("knobs ", t);
                }
            } else if (!strncmp(b, "setlock ", 8)) {
                char arg[32] = "";
                unsigned op = 0x02;
                int retries = 3;
                if (sscanf(b + 8, "%31s %x %d", arg, &op, &retries) >= 1) {
                    int mode = -1;
                    if (isdigit((unsigned char)arg[0])) mode = atoi(arg);
                    else if (!strcasecmp(arg, "unlocked") || !strcasecmp(arg, "open")) mode = 0;
                    else if (!strcasecmp(arg, "keepin") || !strcasecmp(arg, "keep_in") || !strcasecmp(arg, "in")) mode = 1;
                    else if (!strcasecmp(arg, "keepout") || !strcasecmp(arg, "keep_out") || !strcasecmp(arg, "out")) mode = 2;
                    else if (!strcasecmp(arg, "locked") || !strcasecmp(arg, "both")) mode = 3;
                    else if (!strcasecmp(arg, "curfew")) mode = 4;
                    if (mode >= 0 && mode <= 4) queue_writereg1(36, (uint8_t)mode, (uint8_t)op, retries);
                    else hub_log_str("cmd-err ", "bad setlock mode");
                }
            } else if (!strncmp(b, "writereg ", 9)) {
                unsigned reg = 0, val = 0, op = 0x02;
                int retries = 3;
                if (sscanf(b + 9, "%u %i %x %d", &reg, (int *)&val, &op, &retries) >= 2 && reg <= 0xffff && val <= 0xff) {
                    queue_writereg1((uint16_t)reg, (uint8_t)val, (uint8_t)op, retries);
                } else {
                    hub_log_str("cmd-err ", "usage: writereg <reg> <val> [op_hex] [retries]");
                }
            } else if (!strncmp(b, "txraw ", 6)) {
                char hex[140] = "";
                int retries = 2;
                if (sscanf(b + 6, "%139s %d", hex, &retries) >= 1) {
                    uint8_t raw[64];
                    int rlen = parse_hex_bytes(hex, raw, sizeof raw);
                    if (rlen >= 2) {
                        taskENTER_CRITICAL(&cmd_mux);
                        memcpy(cmd_buf, raw, rlen);
                        cmd_len = rlen;
                        cmd_retries = retries > 0 ? retries : 1;
                        cmd_auto_seq = (raw[0] == 0x01 && raw[1] == 0x00);
                        cmd_waiting_ack = false;
                        taskEXIT_CRITICAL(&cmd_mux);
                        hub_log("cmd-queued ", raw, rlen);
                    } else {
                        hub_log_str("cmd-err ", "bad txraw hex");
                    }
                }
            } else if (!strncmp(b, "setkey ", 7)) {
                char h1[16] = "", h2[32] = "", h3[8] = "";
                if (sscanf(b + 7, "%15s %31s %7s", h1, h2, h3) == 3) {
                    uint8_t th[2], tk[8], tc[1];
                    if (parse_hex_bytes(h1, th, 2) == 2 && parse_hex_bytes(h2, tk, 8) == 8 && parse_hex_bytes(h3, tc, 1) == 1) {
                        taskENTER_CRITICAL(&cmd_mux);
                        sess_hdr[0] = th[0]; sess_hdr[1] = th[1];
                        memcpy(sess_k + 4, tk, 8);
                        sess_chk12 = tc[0];
                        sess_valid = true;
                        taskEXIT_CRITICAL(&cmd_mux);
                        log_key_state();
                    }
                }
            } else if (!strncmp(b, "getkey", 6)) {
                log_key_state();
            } else if (!strncmp(b, "cancel", 6)) {
                taskENTER_CRITICAL(&cmd_mux);
                cmd_retries = 0;
                cmd_waiting_ack = false;
                taskEXIT_CRITICAL(&cmd_mux);
                hub_log_str("cmd ", "cancelled");
            } else if (!strncmp(b, "sink ", 5)) {
                char ip[20] = "";
                struct in_addr tmp;
                if (sscanf(b + 5, "%19s", ip) == 1 && inet_pton(AF_INET, ip, &tmp) == 1) {
                    strncpy(g_cfg.sink_ip, ip, sizeof(g_cfg.sink_ip) - 1);
                    cfg_save();
                    update_sink_addrs();
                    hub_log_str("sink ", g_cfg.sink_ip);
                }
            } else if (!strncmp(b, "wifi ", 5)) {
                char ssid[33] = "", pass[65] = "";
                const char *rest = parse_token(b + 5, ssid, sizeof ssid);
                if (rest && ssid[0]) {
                    parse_token(rest, pass, sizeof pass);
                    strncpy(g_cfg.wifi5_ssid, ssid, sizeof(g_cfg.wifi5_ssid) - 1);
                    strncpy(g_cfg.wifi5_pass, pass, sizeof(g_cfg.wifi5_pass) - 1);
                    strncpy(g_cfg.wifi2_ssid, ssid, sizeof(g_cfg.wifi2_ssid) - 1);
                    strncpy(g_cfg.wifi2_pass, pass, sizeof(g_cfg.wifi2_pass) - 1);
                    cfg_save();
                    hub_log_str("wifi-saved ", g_cfg.wifi5_ssid);
                    vTaskDelay(pdMS_TO_TICKS(500));
                    esp_restart();
                }
            } else if (!strcmp(b, "factoryreset")) {
                nvs_handle_t nh;
                if (nvs_open(NVS_NS, NVS_READWRITE, &nh) == ESP_OK) {
                    nvs_erase_all(nh);
                    nvs_commit(nh);
                    nvs_close(nh);
                }
                hub_log_str("cfg ", "erased-rebooting");
                vTaskDelay(pdMS_TO_TICKS(500));
                esp_restart();
            } else if (!strncmp(b, "ota http://", 11)) {
                char expected[48];
                snprintf(expected, sizeof expected, "ota http://%s:", g_cfg.sink_ip);
                if (!strncmp(b, expected, strlen(expected))) {
                    xTaskCreate(ota_task, "ota", 8192, strdup(b + 4), 5, NULL);
                }
            }
        }
        hb++;
        crumb[1] = 3;
        if (wifi_up) stat_msg(s, &stat_to);
        crumb[1] = 4;
    }
}

static void wd_task(void *a)
{
    int down = 0, told = 0;
    esp_task_wdt_add(NULL);
    for (;;) {
        esp_task_wdt_reset();
        crumb[2] = 1;
        vTaskDelay(pdMS_TO_TICKS(5000));
        if (wifi_up && !told) {
            told = 1;
            uint8_t r = (uint8_t)esp_reset_reason();
            hub_log("boot-reset-reason ", &r, 1);
            hub_log("boot-crumbs ", prev_crumb, 4);
            hub_log("prev-wifi-disc ", prev_disc, 2);
            hub_log_str("band ", use5g ? "5GHz" : "2.4GHz");
            hub_log_str("ver ", esp_app_get_description()->version);
            log_key_state();
        }
        static uint32_t last_hb;
        static int stuck;
        if (hb != last_hb) { last_hb = hb; stuck = 0; }
        else if ((stuck += 5) >= 90) { crumb[3] = 2; esp_restart(); }

        down = wifi_up ? 0 : down + 5;
        static int up, checked;
        up = wifi_up ? up + 5 : 0;
        if (up >= 10 && !checked) {
            checked = 1;
            esp_task_wdt_delete(NULL);
            boot_ota_check();
            esp_task_wdt_add(NULL);
        }
        if (up > 60 && up % 120 == 0) {
            if (!use5g) { radio_cmd = 1; vTaskDelay(pdMS_TO_TICKS(700)); }
            esp_task_wdt_delete(NULL);
            boot_ota_check();
            esp_task_wdt_add(NULL);
            if (!use5g) radio_cmd = 2;
        }
        if (up >= 10 && radio_paused) radio_cmd = 2;
        if (up >= 10) start_154();
        if (up == 20) { esp_ota_mark_app_valid_cancel_rollback(); fail5 = 0; }
        if (down >= (use5g ? 90 : 180)) {
            crumb[3] = 1;
            if (fail5 < 250) fail5++;
            esp_restart();
        }
    }
}

void app_main(void)
{
    if (fail5_magic != 0xA5 || esp_reset_reason() != ESP_RST_SW) {
        fail5 = 0;
        fail5_magic = 0xA5;
    }
    use5g = fail5 < 2;
    memcpy(prev_disc, (const void *)&disc_last, 1);
    prev_disc[1] = disc_cnt;
    disc_last = 0;
    disc_cnt = 0;
    memcpy(prev_crumb, (const void *)crumb, 4);
    memset((void *)crumb, 0, 4);
    if (esp_reset_reason() != ESP_RST_SW) memset(prev_crumb, 0, 4);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    cfg_load();
    memcpy(hub_mac, g_cfg.hub_mac, 8);
    if (!cfg_is_ready()) {
        run_setup_portal(0);
    }
    // Recovery guard: after 2x 5 GHz + 1x 2.4 GHz failed boot cycles, open setup portal for 5 min
    // so mistyped Wi-Fi credentials can be fixed without USB, while still auto-retrying after outages.
    if (fail5 >= 3) {
        fail5 = 0;
        run_setup_portal(300);
    }

    q = xQueueCreate(64, sizeof(rxf_t));
    wifi_start();
    xTaskCreate(send_task, "send", 8192, NULL, 6, NULL);
    xTaskCreate(ctrl_task, "ctrl", 6144, NULL, 4, NULL);
    xTaskCreate(wd_task, "wd", 4096, NULL, 3, NULL);
    xTaskCreate(radio_task, "radio", 3072, NULL, 4, NULL);
}

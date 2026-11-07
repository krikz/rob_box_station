/* kws_run — модель ключевых слов колонки на фразе: stdin — s16le 16 кГц моно (фраза от VAD), stdout — лучший класс.
 *
 *   kws_run kws_model.bin [макс_секунд=2.0] [шаг_с=0.1]  <phrase.raw
 *   → "igorek 0.97\n"  (лучшее окно; класс с наибольшей вероятностью среди всех окон, кроме тишины)
 *
 * Признаки — как torchaudio в train.py: окно Ханна (periodic), |rfft|², банк mel, log(x+1e-6), вычитание среднего по
 * времени в окне модели. Сеть — DS-CNN с BatchNorm, слитым в веса (export_c.py). Без зависимостей: gcc -O2 -lm.
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int W, C, NMEL, NFFT, HOP, FRAMES, SR, NBIN;
static float *hann, *fb, *stem_w, *stem_b, *dw_w[4], *dw_b[4], *pw_w[4], *pw_b[4], *fc_w, *fc_b;
static char **labels;

static float *take(float **p, size_t n) { float *r = *p; *p += n; return r; }

static int load(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    char magic[4];
    int32_t h[7];
    if (fread(magic, 1, 4, f) != 4 || memcmp(magic, "KWS1", 4) || fread(h, 4, 7, f) != 7) return -2;
    W = h[0]; C = h[1]; NMEL = h[2]; NFFT = h[3]; HOP = h[4]; FRAMES = h[5]; SR = h[6]; NBIN = NFFT / 2 + 1;
    size_t n = NFFT + (size_t)NBIN * NMEL + W * 25 + W + 4 * ((size_t)W * 9 + W + (size_t)W * W + W) + (size_t)C * W + C;
    float *buf = malloc(n * sizeof(float)), *p = buf;
    if (!buf || fread(buf, sizeof(float), n, f) != n) return -3;
    hann = take(&p, NFFT); fb = take(&p, (size_t)NBIN * NMEL);
    stem_w = take(&p, W * 25); stem_b = take(&p, W);
    for (int k = 0; k < 4; k++) {
        dw_w[k] = take(&p, W * 9); dw_b[k] = take(&p, W);
        pw_w[k] = take(&p, (size_t)W * W); pw_b[k] = take(&p, W);
    }
    fc_w = take(&p, (size_t)C * W); fc_b = take(&p, C);
    int32_t len;
    if (fread(&len, 4, 1, f) != 1) return -4;
    char *text = calloc(len + 1, 1);
    if (fread(text, 1, len, f) != (size_t)len) return -5;
    labels = calloc(C, sizeof(char *));
    for (int i = 0, k = 0; k < C; k++) {
        labels[k] = text + i;
        while (text[i] && text[i] != '\n') i++;
        text[i++] = 0;
    }
    fclose(f);
    return 0;
}

/* log-mel всех кадров фразы: [nframes × NMEL] */
static float *logmel(const float *x, int n, int *nframes) {
    *nframes = n < NFFT ? 0 : 1 + (n - NFFT) / HOP;
    float *out = malloc((size_t)(*nframes > 0 ? *nframes : 1) * NMEL * sizeof(float));
    float *cosv = malloc((size_t)NBIN * NFFT * sizeof(float)), *sinv = malloc((size_t)NBIN * NFFT * sizeof(float));
    float *frame = malloc(NFFT * sizeof(float)), *pw = malloc(NBIN * sizeof(float));
    for (int k = 0; k < NBIN; k++)
        for (int t = 0; t < NFFT; t++) {
            double a = 2 * M_PI * (double)k * t / NFFT;
            cosv[(size_t)k * NFFT + t] = (float)cos(a);
            sinv[(size_t)k * NFFT + t] = (float)sin(a);
        }
    for (int fi = 0; fi < *nframes; fi++) {
        for (int t = 0; t < NFFT; t++) frame[t] = x[fi * HOP + t] * hann[t];
        for (int k = 0; k < NBIN; k++) {  /* ДПФ 400 точек: не степень двойки, считаем прямо */
            float re = 0, im = 0;
            const float *c = cosv + (size_t)k * NFFT, *s = sinv + (size_t)k * NFFT;
            for (int t = 0; t < NFFT; t++) { re += frame[t] * c[t]; im -= frame[t] * s[t]; }
            pw[k] = re * re + im * im;
        }
        for (int m = 0; m < NMEL; m++) {
            float acc = 0;
            for (int k = 0; k < NBIN; k++) acc += pw[k] * fb[(size_t)k * NMEL + m];
            out[(size_t)fi * NMEL + m] = logf(acc + 1e-6f);
        }
    }
    free(cosv); free(sinv); free(frame); free(pw);
    return out;
}

static inline float relu(float v) { return v > 0 ? v : 0; }

/* окно [FRAMES кадров] → вероятности C классов */
static void infer(const float *mel, float *prob) {
    int H = NMEL, T = FRAMES, H2 = (H + 4 - 5) / 2 + 1, T2 = (T + 4 - 5) / 2 + 1, HW = H2 * T2;
    float *in = malloc((size_t)H * T * sizeof(float));
    for (int m = 0; m < H; m++) {  /* [mel][time] + вычитание среднего по времени */
        float mean = 0;
        for (int t = 0; t < T; t++) mean += mel[(size_t)t * NMEL + m];
        mean /= T;
        for (int t = 0; t < T; t++) in[(size_t)m * T + t] = mel[(size_t)t * NMEL + m] - mean;
    }
    float *a = malloc((size_t)W * HW * sizeof(float)), *b = malloc((size_t)W * HW * sizeof(float));
    for (int c = 0; c < W; c++)  /* stem 5×5, шаг 2, поле 2 */
        for (int y = 0; y < H2; y++)
            for (int x = 0; x < T2; x++) {
                float acc = stem_b[c];
                for (int ky = 0; ky < 5; ky++) {
                    int iy = y * 2 + ky - 2;
                    if (iy < 0 || iy >= H) continue;
                    for (int kx = 0; kx < 5; kx++) {
                        int ix = x * 2 + kx - 2;
                        if (ix < 0 || ix >= T) continue;
                        acc += in[(size_t)iy * T + ix] * stem_w[c * 25 + ky * 5 + kx];
                    }
                }
                a[(size_t)c * HW + y * T2 + x] = relu(acc);
            }
    for (int k = 0; k < 4; k++) {
        for (int c = 0; c < W; c++)  /* depthwise 3×3 */
            for (int y = 0; y < H2; y++)
                for (int x = 0; x < T2; x++) {
                    float acc = dw_b[k][c];
                    for (int ky = 0; ky < 3; ky++) {
                        int iy = y + ky - 1;
                        if (iy < 0 || iy >= H2) continue;
                        for (int kx = 0; kx < 3; kx++) {
                            int ix = x + kx - 1;
                            if (ix < 0 || ix >= T2) continue;
                            acc += a[(size_t)c * HW + iy * T2 + ix] * dw_w[k][c * 9 + ky * 3 + kx];
                        }
                    }
                    b[(size_t)c * HW + y * T2 + x] = relu(acc);
                }
        for (int o = 0; o < W; o++) {  /* pointwise 1×1 */
            float *dst = a + (size_t)o * HW;
            for (int i = 0; i < HW; i++) dst[i] = pw_b[k][o];
            for (int c = 0; c < W; c++) {
                float wv = pw_w[k][o * W + c];
                const float *src = b + (size_t)c * HW;
                for (int i = 0; i < HW; i++) dst[i] += wv * src[i];
            }
            for (int i = 0; i < HW; i++) dst[i] = relu(dst[i]);
        }
    }
    float mx = -1e30f, sum = 0;
    for (int o = 0; o < C; o++) {
        float acc = fc_b[o];
        for (int c = 0; c < W; c++) {
            float mean = 0;
            for (int i = 0; i < HW; i++) mean += a[(size_t)c * HW + i];
            acc += fc_w[o * W + c] * mean / HW;
        }
        prob[o] = acc;
        if (acc > mx) mx = acc;
    }
    for (int o = 0; o < C; o++) { prob[o] = expf(prob[o] - mx); sum += prob[o]; }
    for (int o = 0; o < C; o++) prob[o] /= sum;
    free(in); free(a); free(b);
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "kws_run kws_model.bin [max_s] [step_s] <pcm\n"); return 2; }
    int rc = load(argv[1]);
    if (rc) { fprintf(stderr, "модель не загружена (%d)\n", rc); return 2; }
    double max_s = argc > 2 ? atof(argv[2]) : 2.0, step_s = argc > 3 ? atof(argv[3]) : 0.1;
    size_t cap = (size_t)(max_s * SR), n = 0;
    int16_t *pcm = malloc(cap * 2);
    n = fread(pcm, 2, cap, stdin);
    int win = (FRAMES - 1) * HOP + NFFT;
    float *x = calloc(n > (size_t)win ? n : (size_t)win, sizeof(float));
    for (size_t i = 0; i < n; i++) x[i] = pcm[i] / 32768.0f;
    if (n < (size_t)win) n = win;  /* короче окна — добиваем тишиной */
    int nfr;
    float *mel = logmel(x, (int)n, &nfr), *prob = malloc(C * sizeof(float));
    int step = (int)(step_s * SR / HOP + 0.5);
    if (step < 1) step = 1;
    int best_c = 0;
    float best_p = -1;
    for (int s = 0; s + FRAMES <= nfr; s += step) {
        infer(mel + (size_t)s * NMEL, prob);
        for (int c = 0; c < C; c++)
            if (strcmp(labels[c], "_silence_") && prob[c] > best_p) { best_p = prob[c]; best_c = c; }
    }
    printf("%s %.3f\n", labels[best_c], best_p);
    return 0;
}

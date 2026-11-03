#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static long long monotonic_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}
int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "Usage: %s SERVER_IP RATE_MBPS DURATION_SEC PACKET_BYTES\n", argv[0]);
        return 2;
    }
    const char *ip = argv[1];
    double mbps = atof(argv[2]);
    int seconds = atoi(argv[3]);
    int size = atoi(argv[4]);
    if (mbps <= 0 || seconds <= 0 || size < 64) {
        fprintf(stderr, "Rate/duration must be positive and packet size >= 64\n");
        return 2;
    }
    if (size > 1400) size = 1400;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct sockaddr_in dst;
    memset(&dst, 0, sizeof(dst));
    dst.sin_family = AF_INET;
    dst.sin_port = htons(6000);
    if (inet_pton(AF_INET, ip, &dst.sin_addr) != 1) {
        fprintf(stderr, "Invalid server IP\n"); close(fd); return 1;
    }
    char *buf = malloc((size_t)size);
    if (!buf) { perror("malloc"); close(fd); return 1; }
    memset(buf, 'B', (size_t)size);
    double interval_us = ((double)size * 8.0) / (mbps * 1000000.0) * 1000000.0;
    long long start = monotonic_us(), stop = start + (long long)seconds * 1000000LL;
    long long next = start, sent = 0;
    while (monotonic_us() < stop) {
        if (sendto(fd, buf, (size_t)size, 0, (struct sockaddr *)&dst, sizeof(dst)) >= 0)
            sent++;
        next += (long long)interval_us;
        long long now = monotonic_us();
        if (next > now) usleep((useconds_t)(next - now));
    }
    printf("Background packets sent: %lld\n", sent);
    free(buf); close(fd);
    return 0;
}

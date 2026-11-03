#include <arpa/inet.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
typedef long long i64;
#define BUF 512

static i64 now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (i64)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}
static double ms(i64 n) { return (double)n / 1000000.0; }
static i64 absll(i64 n) { return n < 0 ? -n : n; }

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "Usage: %s SERVER_IP CLIENT_ID INITIAL_OFFSET_MS ROUNDS\n", argv[0]);
        return 2;
    }
    const char *server_ip = argv[1];
    int client_id = atoi(argv[2]);
    double initial_offset_ms = atof(argv[3]);
    int rounds = atoi(argv[4]);
    if (rounds < 1) rounds = 1;

    /* Simulated logical clock offset; the OS clock is never changed. */
    i64 bias_ns = (i64)(initial_offset_ms * 1000000.0);
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct timeval tv = { .tv_sec = 2, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in server;
    memset(&server, 0, sizeof(server));
    server.sin_family = AF_INET;
    server.sin_port = htons(5005);
    if (inet_pton(AF_INET, server_ip, &server.sin_addr) != 1) {
        fprintf(stderr, "Invalid server IP\n"); close(fd); return 1;
    }

    printf("client_id,seq,rtt_ms,estimated_server_minus_client_ms,known_server_minus_client_ms,offset_estimation_error_ms,post_correction_error_ms\n");
    for (int seq = 1; seq <= rounds; seq++) {
        i64 t1 = now_ns() + bias_ns;
        char req[BUF];
        snprintf(req, sizeof(req), "SYNC %d %d %lld", client_id, seq, t1);
        if (sendto(fd, req, strlen(req), 0,
                   (struct sockaddr *)&server, sizeof(server)) < 0) {
            perror("sendto"); continue;
        }

        char reply[BUF];
        ssize_t n = recvfrom(fd, reply, sizeof(reply)-1, 0, NULL, NULL);
        if (n < 0) {
            printf("%d,%d,TIMEOUT,,,,\n", client_id, seq);
            fflush(stdout); usleep(300000); continue;
        }
        reply[n] = '\0';
        i64 t4 = now_ns() + bias_ns;
        int rid, rseq;
        i64 rt1, t2, t3;
        if (sscanf(reply, "REPLY %d %d %lld %lld %lld",
                   &rid, &rseq, &rt1, &t2, &t3) != 5 ||
            rid != client_id || rseq != seq) {
            fprintf(stderr, "Unexpected response: %s\n", reply);
            continue;
        }

        i64 rtt = (t4 - t1) - (t3 - t2);
        /* Offset is server clock minus client clock. */
        i64 offset = ((t2 - t1) + (t3 - t4)) / 2;
        i64 known_offset = -bias_ns;
        i64 estimate_error = absll(offset - known_offset);

        /* Apply estimated offset to this simulated logical clock. */
        bias_ns += offset;
        i64 remaining_bias = bias_ns;
        i64 post_error = absll(remaining_bias);

        printf("%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f\n",
               client_id, seq, ms(rtt), ms(offset), ms(known_offset),
               ms(estimate_error), ms(post_error));
        fflush(stdout);
        usleep(500000);
    }
    close(fd);
    return 0;
}

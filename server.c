#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define SYNC_PORT 5005
#define BG_PORT 6000
#define BUF 512
typedef long long i64;

static i64 now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (i64)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

typedef struct {
    int fd;
    struct sockaddr_in peer;
    socklen_t peer_len;
    char request[BUF];
    i64 t2;
} Job;

static void *answer_request(void *arg) {
    Job *j = (Job *)arg;
    unsigned int seq;
    int client_id;
    i64 t1;
    if (sscanf(j->request, "SYNC %d %u %lld", &client_id, &seq, &t1) == 3) {
        i64 t3 = now_ns();
        char reply[BUF];
        int n = snprintf(reply, sizeof(reply), "REPLY %d %u %lld %lld %lld",
                         client_id, seq, t1, j->t2, t3);
        sendto(j->fd, reply, (size_t)n, 0,
               (struct sockaddr *)&j->peer, j->peer_len);
        printf("SYNC client=%d seq=%u from=%s\n",
               client_id, seq, inet_ntoa(j->peer.sin_addr));
        fflush(stdout);
    }
    free(j);
    return NULL;
}

static void *background_receiver(void *unused) {
    (void)unused;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("background socket"); return NULL; }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(BG_PORT);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("background bind"); close(fd); return NULL;
    }
    char buf[2048];
    printf("Background UDP receiver listening on %d\n", BG_PORT);
    fflush(stdout);
    while (1) {
        ssize_t n = recvfrom(fd, buf, sizeof(buf), 0, NULL, NULL);
        if (n < 0 && errno != EINTR) perror("background recvfrom");
    }
    return NULL;
}

int main(void) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(SYNC_PORT);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); close(fd); return 1;
    }
    printf("Time server listening on UDP %d\n", SYNC_PORT);
    fflush(stdout);
    pthread_t bg;
    if (pthread_create(&bg, NULL, background_receiver, NULL) == 0)
        pthread_detach(bg);

    while (1) {
        Job *j = calloc(1, sizeof(*j));
        if (!j) { perror("calloc"); continue; }
        j->fd = fd;
        j->peer_len = sizeof(j->peer);
        ssize_t n = recvfrom(fd, j->request, sizeof(j->request)-1, 0,
                             (struct sockaddr *)&j->peer, &j->peer_len);
        if (n < 0) { perror("recvfrom"); free(j); continue; }
        j->request[n] = '\0';
        j->t2 = now_ns();
        pthread_t th;
        if (pthread_create(&th, NULL, answer_request, j) == 0)
            pthread_detach(th);
        else { perror("pthread_create"); free(j); }
    }
    close(fd);
    return 0;
}

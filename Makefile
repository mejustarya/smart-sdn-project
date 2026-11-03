CC = gcc
CFLAGS = -O2 -Wall -Wextra
LDLIBS_CLIENT = -lm
LDLIBS_SERVER = -pthread

.PHONY: all clean
all: time_client time_server background_sender

time_client: client.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS_CLIENT)

time_server: server.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS_SERVER)

background_sender: background_sender.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f time_client time_server background_sender

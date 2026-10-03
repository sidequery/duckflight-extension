#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *env(const char *name) {
    const char *value = getenv(name);
    return value ? value : "";
}

static void sql_string(FILE *output, const char *value) {
    fputc('\'', output);
    for (; *value; value++) {
        if (*value == '\'') fputc('\'', output);
        fputc(*value, output);
    }
    fputc('\'', output);
}

static void setting(FILE *output, const char *variable, const char *name) {
    const char *value = env(variable);
    if (!*value) return;
    fprintf(output, "set %s=", name);
    sql_string(output, value);
    fputs(";\n", output);
}

static void serve(FILE *output, const char *protocol, const char *function,
                  const char *address, const char *config) {
    if (!*address) return;
    fprintf(output, "select 'DuckFlight %s listening on ' || address from %s(",
            protocol, function);
    sql_string(output, address);
    fputc(',', output);
    sql_string(output, config);
    fputs(");\n", output);
}

static int readable_file(const char *path) {
    struct stat info;
    return stat(path, &info) == 0 && S_ISREG(info.st_mode) && access(path, R_OK) == 0;
}

int main(void) {
    const char *database = env("DUCKFLIGHT_DATABASE");
    const char *config = env("DUCKFLIGHT_CONFIG");
    const char *pg = env("DUCKFLIGHT_PG_ADDRESS");
    const char *flight = env("DUCKFLIGHT_FLIGHT_ADDRESS");
    const char *init = env("DUCKFLIGHT_INIT_SQL");
    if (!*database || *database == '-') {
        fputs("DUCKFLIGHT_DATABASE must be a database path or :memory:\n", stderr);
        return 1;
    }
    if (!readable_file(config)) {
        fprintf(stderr, "mount readable authentication/TLS config at %s\n", config);
        return 1;
    }
    if (!*pg && !*flight) {
        fputs("enable at least one listener address\n", stderr);
        return 1;
    }
    FILE *init_sql = *init ? fopen(init, "r") : NULL;
    if (*init && (!readable_file(init) || !init_sql)) {
        fprintf(stderr, "cannot read startup SQL at %s\n", init);
        if (init_sql) fclose(init_sql);
        return 1;
    }

    // Block before launching so a signal cannot fall between checking and waiting.
    sigset_t signals, previous_mask;
    sigemptyset(&signals);
    sigaddset(&signals, SIGTERM);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGCHLD);
    if (sigprocmask(SIG_BLOCK, &signals, &previous_mask)) {
        perror("block lifecycle signals");
        return 1;
    }
    signal(SIGPIPE, SIG_IGN);
    int input[2];
    if (pipe(input)) {
        perror("CLI input pipe");
        return 1;
    }
    pid_t child = fork();
    if (child < 0) {
        perror("launch CLI");
        return 1;
    }
    if (child == 0) {
        sigprocmask(SIG_SETMASK, &previous_mask, NULL);
        close(input[1]);
        if (dup2(input[0], STDIN_FILENO) < 0) _exit(1);
        close(input[0]);
        execl("/usr/local/bin/duckdb", "duckdb", "-unsigned", "-bail", "-noheader",
              "-list", "-init", "/dev/null", database, (char *)NULL);
        perror("execute DuckDB CLI");
        _exit(1);
    }
    close(input[0]);
    FILE *commands = fdopen(input[1], "w");
    if (!commands) {
        perror("open CLI input");
        close(input[1]);
        waitpid(child, NULL, 0);
        return 1;
    }
    fputs("load '/opt/duckflight/duckflight.duckdb_extension';\n"
          "select case when loaded then 'DuckFlight core loaded' else error(detail) end "
          "from duckflight_core_status();\n", commands);
    setting(commands, "DUCKFLIGHT_MEMORY_LIMIT", "memory_limit");
    setting(commands, "DUCKFLIGHT_THREADS", "threads");
    setting(commands, "DUCKFLIGHT_TEMP_DIRECTORY", "temp_directory");
    if (init_sql) {
        char buffer[4096];
        size_t length;
        while ((length = fread(buffer, 1, sizeof(buffer), init_sql))) {
            fwrite(buffer, 1, length, commands);
        }
        int failed = ferror(init_sql);
        fclose(init_sql);
        if (failed) {
            fputs("failed reading startup SQL\n", stderr);
            fclose(commands);
            waitpid(child, NULL, 0);
            return 1;
        }
        fputs("\n;\n", commands);
    }
    // Trusted startup may load extensions and allowlist dedicated data mounts.
    // Apply the shared SQL boundary before either network listener is reachable.
    // PgWire initializes each session's timezone through DuckDB configuration.
    fputs("set allowed_configs=['TimeZone'];\n"
          "set enable_external_access=false;\n"
          "set lock_configuration=true;\n", commands);
    serve(commands, "pgwire", "duckflight_pg_serve", pg, config);
    serve(commands, "flight", "duckflight_flight_serve", flight, config);
    fputs(".print DuckFlight ready\n", commands);
    fflush(commands);

    int status;
    for (;;) {
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) goto finished;
        if (result < 0 && errno != EINTR) {
            perror("wait for CLI");
            fclose(commands);
            return 1;
        }
        int signal_number;
        int error = sigwait(&signals, &signal_number);
        if (error) {
            errno = error;
            perror("wait for lifecycle signal");
            fclose(commands);
            return 1;
        }
        if (signal_number != SIGCHLD) break;
    }
    // Closing the CLI database destroys the core and stops all of its listeners.
    fputs(".quit\n", commands);
    fclose(commands);
    commands = NULL;
    while (waitpid(child, &status, 0) != child) {
        if (errno != EINTR) {
            perror("wait for CLI shutdown");
            return 1;
        }
    }
finished:
    if (commands) fclose(commands);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}

#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Binary launcher intentionally installed as run.sh. Cinnamon/Nemo recognizes
 * it as an application instead of prompting whether to display or run a text
 * shell script. The Python program remains next to this executable. */
int main(int argc, char **argv) {
    char executable[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (length < 0) {
        if (realpath(argv[0], executable) == NULL) {
            fprintf(stderr, "Corne Keymap Editor: cannot locate launcher: %s\n", strerror(errno));
            return 1;
        }
        length = (ssize_t)strlen(executable);
    }
    executable[length] = '\0';

    char *slash = strrchr(executable, '/');
    if (slash == NULL) {
        fprintf(stderr, "Corne Keymap Editor: invalid launcher path\n");
        return 1;
    }
    *slash = '\0';

    char script[PATH_MAX];
    if (snprintf(script, sizeof(script), "%s/corne_keymap_editor.py", executable) >= (int)sizeof(script)) {
        fprintf(stderr, "Corne Keymap Editor: application path is too long\n");
        return 1;
    }
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "Corne Keymap Editor: cannot read %s\n", script);
        return 1;
    }

    char **python_argv = calloc((size_t)argc + 2, sizeof(char *));
    if (python_argv == NULL) {
        fprintf(stderr, "Corne Keymap Editor: out of memory\n");
        return 1;
    }
    python_argv[0] = "python3";
    python_argv[1] = script;
    for (int index = 1; index < argc; ++index) {
        python_argv[index + 1] = argv[index];
    }
    python_argv[argc + 1] = NULL;

    execvp("python3", python_argv);
    fprintf(stderr, "Corne Keymap Editor: cannot start Python 3: %s\n", strerror(errno));
    free(python_argv);
    return 1;
}

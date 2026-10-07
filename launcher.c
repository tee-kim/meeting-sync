// 전체 디스크 접근 권한을 받는 작은 실행 파일.
// python 전체가 아니라 이 파일에만 권한을 주고, 자식 프로세스(meeting_sync.py)가 권한을 상속받는다.
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>

#ifndef SCRIPT_PATH
#error "SCRIPT_PATH must be defined"
#endif

extern char **environ;

int main(int argc, char *argv[]) {
    char *args[argc + 2];
    args[0] = "/usr/bin/python3";
    args[1] = SCRIPT_PATH;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;

    pid_t pid;
    int rc = posix_spawn(&pid, args[0], NULL, NULL, args, environ);
    if (rc != 0) {
        fprintf(stderr, "posix_spawn failed: %d\n", rc);
        return 127;
    }
    int status;
    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        return 1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

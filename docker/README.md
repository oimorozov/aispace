# Sandbox Codex в Docker

`codex-seccomp.json` основан на [стандартном профиле Moby](https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json), версия `245180c51918481c0525424b3ee025d2b435d46c`. Лицензия Apache 2.0 сохранена в `LICENSE.moby-profiles`.

Изменения aispace: разрешены `unshare`, `mount`, `umount2`, `pivot_root`. Для `clone` добавлено разрешение только при установленном флаге `CLONE_NEWUSER`. Эти операции нужны bubblewrap для создания вложенного sandbox. Сохранены запрет остальных системных вызовов по умолчанию и ограничения исходного профиля; дополнительные Linux capabilities не выдаются. Поля `comment` удалены.

Профиль применяется только к backend. Он позволяет Codex исполнять политики `read-only` и `workspace-write` внутри обычного контейнера без `privileged`, `seccomp=unconfined` или отключения sandbox Codex. Режим совместимости `use_legacy_landlock` не используется: Codex 0.155.0 отказывается исполнять с ним политику workspace-write с защищёнными подпапками.

Проверено в OrbStack / Docker 29.4.0: read-only запрещает запись; workspace-write разрешает запись в выбранную папку, запрещает запись вне неё и в `.git` / `.codex`; создание сетевого сокета sandbox-командой запрещено в обоих режимах. Для совместимости с другими ядрами Linux требуется поддержка непривилегированных user namespaces.

Источники: [Docker seccomp](https://docs.docker.com/engine/security/seccomp/), [Codex sandbox](https://learn.chatgpt.com/docs/sandboxing), [Codex permissions](https://learn.chatgpt.com/docs/permissions).

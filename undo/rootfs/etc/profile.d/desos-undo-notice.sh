# DesOS undo: tell an interactive login what automatic recovery did.
case "$-" in
    *i*)
        if [ -s /.snapshots/.desos-undo/notices.json ] && [ -x /usr/sbin/desos-undo ]; then
            /usr/sbin/desos-undo notices 2>/dev/null | grep -v '^No notices\.$' || true
        fi
        ;;
esac

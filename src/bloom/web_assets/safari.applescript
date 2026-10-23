-- Only the window created by this search is addressed. No cookie/history export.
on run argv
    set action to item 1 of argv
    tell application "Safari"
        if action is "open" then
            make new document with properties {URL:"about:blank"}
            set w to front window
            set bounds of w to {50, 50, 562, 1000}
            return id of w as text
        end if
        set w to first window whose id is (item 2 of argv as integer)
        if (count of tabs of w) is not 1 then error "Search window was modified; leaving it untouched"
        if action is "navigate" then
            set URL of current tab of w to item 3 of argv
            return "navigating"
        else if action is "eval" then
            set js to read POSIX file (item 3 of argv) as «class utf8»
            return do JavaScript js in current tab of w
        else if action is "resize" then
            set bounds of w to {50, 50, 50 + (item 3 of argv as integer), 50 + (item 4 of argv as integer)}
            return "resized"
        else if action is "close" then
            if (URL of current tab of w) is not (item 3 of argv) then error "Search window URL changed; leaving it untouched"
            close w
            return "closed"
        else if action is "url" then
            return URL of current tab of w
        end if
        error "Unknown Safari bridge action"
    end tell
end run

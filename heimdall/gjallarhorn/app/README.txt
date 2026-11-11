GJALLARHORN - Heimdall's alerts on a trader's screen
====================================================

Nothing to install: Windows PowerShell and WPF are part of Windows.

Set it up on a trader's PC (once, as that trader, no admin rights):
double-click install-gjallarhorn.cmd in this folder. It asks for

  * the shared folder Heimdall writes the alerts to: the one in Bifrost,
    Settings > Gjallarhorn (Enter keeps the folder this app folder is in), and
  * the desks this PC is for, comma separated. An alert pops up when it is
    meant for everyone, for this Windows user name, or for one of these desks.

Or in one line, without the questions:

    "\\server\share\Gjallarhorn\app\install-gjallarhorn.cmd" "\\server\share\Gjallarhorn" "Equities,Rates"

Gjallarhorn then starts at every logon. The other command files here:

    start-gjallarhorn.cmd       start it now (after it was stopped)
    stop-gjallarhorn.cmd        quit it now; it starts again at the next logon
    uninstall-gjallarhorn.cmd   quit it and stop it starting at logon
                                (install-gjallarhorn.cmd puts it back)

Run install-gjallarhorn.cmd again to change the folder or the desks.

What the trader gets:
  * one card at the bottom right, never a stack: it shows the most serious
    alert first, with "1 of 4" and arrows to step through the others.
    Acknowledge moves on to the next; Details opens the list; the cross hides
    the card (the alerts stay in the list). The card fades on its own after
    a while unless something in it is critical.
  * an icon by the clock with the number of alerts to acknowledge. Click it
    (or "Gjallarhorn" in the Start menu) for the list: go through the alerts
    with Previous / Next, see every row, copy the rows to Excel, acknowledge
    one or all, pause pop-ups for an hour.
  * closing the list only minimizes it. Gjallarhorn stops only with Quit, in
    the list or in the icon's menu (it asks first). Restart, in the same
    places, starts it again from the share, with any new version.
  * a horn call with each pop-up, more urgent with the severity (or the
    alert's own sound); 'Sounds' in the icon's menu turns them off on this PC.

An acknowledgement is written to the share; Heimdall records who and when, and
the alert leaves every other trader's card too.

Its log:    %LOCALAPPDATA%\Gjallarhorn\gjallarhorn.log (each start says which folder it reads)

This folder is published from Bifrost (Settings > Gjallarhorn). Publishing a
new version updates everyone at their next logon, or when they press Restart.

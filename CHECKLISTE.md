# Trading-Bot mit ChatGPT oder Claude gebaut? Die Prüfliste vor dem Livestart

> **Keine Anlageberatung, ohne Gewähr, zuerst im Papierkonto testen.** Diese Prüfliste und der Code in diesem Repository dienen der allgemeinen Information. Wer alle Punkte erfüllt, senkt das Risiko technischer Fehler; über die Strategie sagt die Prüfliste nichts. Automatisierter Handel kann Geld verlieren, mit Hebel je nach Produkt auch mehr als den Einsatz.

ChatGPT, Claude und Coding-Agenten wie Claude Code oder Codex können lauffähige Trading-Bots schreiben, von der Strategielogik über den Backtest bis zur Anbindung an Interactive Brokers oder MetaTrader 5. Ob dieser Code mit echtem Geld laufen sollte, beantworten sie nicht. Gerade die teuren Fehler sehen fertig aus: Look-ahead-Bias, fehlende Kosten, eine doppelte Order oder eine Position, von der der Bot nichts weiß.

Diese Prüfliste übersetzt den Schritt von „läuft“ zu „handelt live“ in 20 Punkte mit klaren Kriterien. Jedes Kriterium lässt sich prüfen, ohne der Zusammenfassung des Assistenten zu vertrauen. Geprüft werden der Code und die Umgebung, die tatsächlich live gehen.

Stand: November 2026. Die Werkzeuge ändern sich schnell; prüfen Sie Ihre Einstellungen nach jedem größeren Update erneut.

## Zugang und Werkzeuge

| Nr. | Prüfpunkt | Bestanden, wenn … |
|---|---|---|
| 1 | Zugangsdaten | Kein Live-Schlüssel, kein Passwort und kein Token liegt dort, wo der Assistent arbeitet; ein Secret-Scan über Repository und Historie findet nichts. |
| 2 | Keine Order-Werkzeuge | Beim Entwickeln ist kein MCP-Server mit Order-Funktionen verbunden; IBKR-Sitzungen, die Werkzeuge erreichen, laufen mit „Read-Only API“. |
| 3 | Regeln technisch durchgesetzt | Deny-Regeln oder Hooks schützen Geheimnisse und Testdateien, die Sandbox ist an, Pushes auf den Hauptbranch brauchen eine Freigabe; Vollzugriff nur in einem isolierten Container ohne Zugangsdaten. |
| 4 | Mensch liest Order- und Risikocode | Eine Person erklärt jeden Orderweg ohne den Assistenten: Einstieg, Ausstieg, Stop, Storno, Glattstellen. |
| 5 | Abhängigkeiten | Versionen sind festgelegt; jedes vom Assistenten ergänzte Paket wird gepflegt und tatsächlich genutzt. |

## Daten und Backtest

| Nr. | Prüfpunkt | Bestanden, wenn … |
|---|---|---|
| 6 | Look-ahead-Bias | Der Präfix-Test besteht für jedes Signal und jeden Filter auf langer Historie; ausgeführt wird frühestens zur nächsten Bar. |
| 7 | Zeitstempel | Ein Test mit konstruierten Bars zeigt, ob Zeitstempel die Eröffnung oder den Schluss einer Bar markieren. |
| 8 | Kosten | Ein Test mit einem einzigen Trade liefert genau das erwartete Netto-Ergebnis; ohne Kostenmodell bricht der Backtest ab. |
| 9 | Überanpassung | Die Zahl der getesteten Parametersätze ist protokolliert; die gewählten Parameter sind auf Daten geprüft, die nicht zur Auswahl dienten. |
| 10 | Zeitzonen | Zeiten sind in UTC gespeichert; Handelszeitfilter bestehen Tests rund um die Zeitumstellung. |

## Orders und Zustand

| Nr. | Prüfpunkt | Bestanden, wenn … |
|---|---|---|
| 11 | Einheiten | Risiko wird je Instrument korrekt in Stückzahl umgerechnet; Preise sind auf die Tickgröße gerundet, Mengen auf zulässige Schritte. |
| 12 | Ein Signal, eine Order | Kommt dasselbe Signal nach einer Zeitüberschreitung erneut, entsteht keine zweite Order. |
| 13 | Neustart | Nach einem harten Abbruch mit offener Position und offener Order lädt der Bot Positionen und Orders, bevor er etwas sendet. |
| 14 | Positionsabgleich | Bot und Broker stimmen beim Start und regelmäßig überein; eine Abweichung sperrt neue Orders und löst einen Alarm aus. |
| 15 | Teilausführung, Ablehnung, Storno | Tests spielen jeden Fall durch; danach stimmt der interne Zustand mit dem simulierten Broker überein. |
| 16 | Verbindungsabbruch | Bei IBKR-Code 1100 gehen keine neuen Orders hinaus, nach 1101 oder 1102 erst nach dem Abgleich. |

## Risiko und Betrieb

| Nr. | Prüfpunkt | Bestanden, wenn … |
|---|---|---|
| 17 | Limits vor jeder Order | Höchstmenge, Höchstwert, Preisband und Tagesverlust werden im Code geprüft; eine erreichte Grenze gilt auch nach einem Neustart. |
| 18 | Notaus | Eine Aktion sperrt neue Orders und storniert offene, wirkt ohne den Strategieprozess und wurde im Paper-Konto einmal ausgelöst. |
| 19 | Alarme und Protokoll | Ein Testalarm erreicht das Handy; jede Entscheidung steht mit UTC-Zeit, Order-ID und Broker-Antwort im Protokoll, ohne Zugangsdaten. |
| 20 | Paper-Phase und Kontoprüfung | Austrittskriterien sind vorher festgelegt und erfüllt; der Bot prüft beim Start Kontonummer und Kontoart, den Wechsel auf live vollzieht ein Mensch. |

## Was der Python-Code in diesem Repository abdeckt

Die Bausteine in `preflight/` unterstützen einige Punkte technisch, ersetzen aber keinen davon vollständig:

- **10 Zeitzonen:** `check_freshness` und `check_bar_sequence` lehnen Zeitstempel ohne Zeitzone ab und melden veraltete Daten, Lücken, Duplikate und Bars in falscher Reihenfolge.
- **12 Ein Signal, eine Order:** `OrderGuard` sperrt jede bereits verwendete `client_order_id`.
- **13 Neustart und 14 Positionsabgleich:** `register_open_order`, `set_position`, `reconcile_positions` und `recommend_action`, das bei jeder Abweichung `HALT` empfiehlt.
- **17 Limits vor jeder Order:** Höchstposition je Symbol, Brutto-Exposure, Orders pro Minute und Tagesverlust. Ein erreichter Tagesverlust löst den Notaus aus, der mit Auslöse-Datei einen Neustart übersteht. Ein Preisband fehlt.
- **18 Notaus:** `KillSwitch` sperrt neue Orders, bis ein Mensch ihn zurücksetzt. Offene Orders storniert der Broker-Adapter.
- **16 Verbindungsabbruch und 20 Kontoprüfung:** nur im Beispiel `examples/ib_async_adapter.py`, das nicht gegen TWS oder IB Gateway getestet ist.

Einzelheiten und Grenzen stehen im [README](README.md) (englisch).

## Weiterlesen

- Der Artikel mit Erläuterungen und Quellen: [Trading-Bot mit ChatGPT oder Claude gebaut? Die Prüfliste](https://marcelgautsche.de/insights/trading-bot-ki-generiert-pruefliste.html)
- Eine unabhängige Prüfung Ihres Codes: [Code-Review und Systemanalyse für Trading-Systeme](https://marcelgautsche.de/leistungen/code-review-trading-systeme.html). Geprüft werden Software und Methodik, nicht die Erfolgsaussichten der Strategie.

Dieses Repository ist KI-gestützt entstanden (Claude). Marcel Gautsche verantwortet die Veröffentlichung; was geprüft ist, zeigen die Tests.

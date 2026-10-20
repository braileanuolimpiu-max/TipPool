"""The guest tip page in the guest's language. Guests are often tourists; managers stay in English.

Picked from ?lang=, then the guest's earlier choice (a cookie), then their browser's Accept-Language.
A plain table instead of gettext, so nothing has to be compiled on the server.
"""
LANGS = {"en": "English", "es": "Español", "fr": "Français", "de": "Deutsch", "it": "Italiano", "pt": "Português",
         "ro": "Română"}
COOKIE = "tp_lang"

STRINGS = {
    "en": {
        "title": "Leave a tip", "thanks": "Thank you", "table": "Table",
        "pool_note": "Thank you. Your tip is shared with the whole team who looked after you today.",
        "who_personal": "Who would you like to tip?", "who_both": "Who is this tip for?", "whole_team": "The whole team",
        "note_both": "Tips for the whole team are shared fairly. A tip for one person goes to them in full.",
        "note_personal": "Your whole tip goes to the person you choose.",
        "cancelled": "No tip was taken. Choose an amount to try again.",
        "bill": "Your bill", "tip_percent": "Tip percent", "tip_amount": "Tip amount", "other": "Other amount",
        "nobody": "Nobody is taking tips here right now. Please ask the staff.",
        "loading": "Loading PayPal…",
        "guest_note": "Payments are processed by PayPal. The business never sees your card details.",
        "language": "Language", "privacy": "Privacy",
        "err_bill": "Enter your bill to tip a percent, or type an amount.",
        "err_amount": "Please choose a tip amount.", "err_who": "Please choose who you'd like to tip.",
        "err_start": "Could not start the payment.", "completing": "Completing your tip…",
        "err_generic": "Something went wrong.", "pay_cancelled": "Payment cancelled. You can try again.",
        "err_paypal": "PayPal error. Please try again.",
        "err_load": "PayPal could not be loaded. Please refresh the page.",
        "tip": "Tip", "paid": "PAID",
        "done_note": "Thank you. At the end of the shift it's split between everyone who worked and paid straight to "
                     "their PayPal.",
        "done_personal": "Thank you. It goes in full to {name}, paid straight to their PayPal at the end of the shift.",
        "confirming": "Your {amount} tip is being confirmed by PayPal. You can close this page.",
        "not_found": "We couldn't find this tip. If you were charged, your receipt from PayPal is on its way.",
        "another": "Leave another tip", "close_page": "You can close this page.",
    },
    "es": {
        "title": "Dejar una propina", "thanks": "Gracias", "table": "Mesa",
        "pool_note": "Gracias. Tu propina se reparte con todo el equipo que te atendió hoy.",
        "who_personal": "¿A quién quieres dar la propina?", "who_both": "¿Para quién es esta propina?",
        "whole_team": "Todo el equipo",
        "note_both": "Las propinas para todo el equipo se reparten de forma justa. Una propina para una persona es "
                     "íntegra para ella.",
        "note_personal": "Toda tu propina va a la persona que elijas.",
        "cancelled": "No se cobró ninguna propina. Elige un importe para intentarlo de nuevo.",
        "bill": "Tu cuenta", "tip_percent": "Porcentaje de propina", "tip_amount": "Importe de la propina",
        "other": "Otro importe",
        "nobody": "Ahora mismo nadie está recibiendo propinas aquí. Pregunta al personal.",
        "loading": "Cargando PayPal…",
        "guest_note": "Los pagos los procesa PayPal. El negocio nunca ve los datos de tu tarjeta.",
        "language": "Idioma", "privacy": "Privacidad",
        "err_bill": "Introduce tu cuenta para dar un porcentaje, o escribe un importe.",
        "err_amount": "Elige el importe de la propina.", "err_who": "Elige a quién quieres dar la propina.",
        "err_start": "No se pudo iniciar el pago.", "completing": "Completando tu propina…",
        "err_generic": "Algo salió mal.", "pay_cancelled": "Pago cancelado. Puedes intentarlo de nuevo.",
        "err_paypal": "Error de PayPal. Inténtalo de nuevo.",
        "err_load": "No se pudo cargar PayPal. Actualiza la página.",
        "tip": "Propina", "paid": "PAGADO",
        "done_note": "Gracias. Al final del turno se reparte entre todos los que trabajaron y se paga directamente a "
                     "su PayPal.",
        "done_personal": "Gracias. Va íntegra a {name}, pagada directamente a su PayPal al final del turno.",
        "confirming": "PayPal está confirmando tu propina de {amount}. Puedes cerrar esta página.",
        "not_found": "No encontramos esta propina. Si se te cobró, tu recibo de PayPal está en camino.",
        "another": "Dejar otra propina", "close_page": "Puedes cerrar esta página.",
    },
    "fr": {
        "title": "Laisser un pourboire", "thanks": "Merci", "table": "Table",
        "pool_note": "Merci. Votre pourboire est partagé avec toute l'équipe qui s'est occupée de vous aujourd'hui.",
        "who_personal": "À qui voulez-vous laisser un pourboire ?", "who_both": "Pour qui est ce pourboire ?",
        "whole_team": "Toute l'équipe",
        "note_both": "Les pourboires pour toute l'équipe sont partagés équitablement. Un pourboire pour une personne "
                     "lui revient en entier.",
        "note_personal": "Tout votre pourboire va à la personne choisie.",
        "cancelled": "Aucun pourboire n'a été pris. Choisissez un montant pour réessayer.",
        "bill": "Votre addition", "tip_percent": "Pourcentage du pourboire", "tip_amount": "Montant du pourboire",
        "other": "Autre montant",
        "nobody": "Personne ne reçoit de pourboires ici pour le moment. Demandez au personnel.",
        "loading": "Chargement de PayPal…",
        "guest_note": "Les paiements sont traités par PayPal. L'établissement ne voit jamais vos données de carte.",
        "language": "Langue", "privacy": "Confidentialité",
        "err_bill": "Saisissez votre addition pour donner un pourcentage, ou tapez un montant.",
        "err_amount": "Choisissez le montant du pourboire.",
        "err_who": "Choisissez à qui vous voulez laisser un pourboire.",
        "err_start": "Impossible de lancer le paiement.", "completing": "Finalisation de votre pourboire…",
        "err_generic": "Une erreur s'est produite.", "pay_cancelled": "Paiement annulé. Vous pouvez réessayer.",
        "err_paypal": "Erreur PayPal. Veuillez réessayer.",
        "err_load": "PayPal n'a pas pu être chargé. Actualisez la page.",
        "tip": "Pourboire", "paid": "PAYÉ",
        "done_note": "Merci. À la fin du service, il est partagé entre toutes les personnes qui ont travaillé et "
                     "versé directement sur leur PayPal.",
        "done_personal": "Merci. Il revient en entier à {name}, versé directement sur son PayPal à la fin du service.",
        "confirming": "PayPal confirme votre pourboire de {amount}. Vous pouvez fermer cette page.",
        "not_found": "Nous n'avons pas trouvé ce pourboire. Si vous avez été débité, votre reçu PayPal arrive.",
        "another": "Laisser un autre pourboire", "close_page": "Vous pouvez fermer cette page.",
    },
    "de": {
        "title": "Trinkgeld geben", "thanks": "Danke", "table": "Tisch",
        "pool_note": "Danke. Ihr Trinkgeld wird mit dem ganzen Team geteilt, das Sie heute betreut hat.",
        "who_personal": "Wem möchten Sie Trinkgeld geben?", "who_both": "Für wen ist dieses Trinkgeld?",
        "whole_team": "Das ganze Team",
        "note_both": "Trinkgeld für das ganze Team wird fair geteilt. Trinkgeld für eine Person geht vollständig an "
                     "sie.",
        "note_personal": "Ihr ganzes Trinkgeld geht an die Person, die Sie wählen.",
        "cancelled": "Es wurde kein Trinkgeld abgebucht. Wählen Sie einen Betrag, um es erneut zu versuchen.",
        "bill": "Ihre Rechnung", "tip_percent": "Trinkgeld in Prozent", "tip_amount": "Trinkgeldbetrag",
        "other": "Anderer Betrag",
        "nobody": "Hier nimmt gerade niemand Trinkgeld entgegen. Bitte fragen Sie das Personal.",
        "loading": "PayPal wird geladen…",
        "guest_note": "Zahlungen werden von PayPal abgewickelt. Das Geschäft sieht Ihre Kartendaten nie.",
        "language": "Sprache", "privacy": "Datenschutz",
        "err_bill": "Geben Sie Ihre Rechnung ein, um einen Prozentsatz zu geben, oder tippen Sie einen Betrag.",
        "err_amount": "Bitte wählen Sie einen Trinkgeldbetrag.", "err_who": "Bitte wählen Sie, wem Sie Trinkgeld geben.",
        "err_start": "Die Zahlung konnte nicht gestartet werden.", "completing": "Ihr Trinkgeld wird abgeschlossen…",
        "err_generic": "Etwas ist schiefgelaufen.",
        "pay_cancelled": "Zahlung abgebrochen. Sie können es erneut versuchen.",
        "err_paypal": "PayPal-Fehler. Bitte versuchen Sie es erneut.",
        "err_load": "PayPal konnte nicht geladen werden. Bitte laden Sie die Seite neu.",
        "tip": "Trinkgeld", "paid": "BEZAHLT",
        "done_note": "Danke. Am Ende der Schicht wird es unter allen aufgeteilt, die gearbeitet haben, und direkt auf "
                     "ihr PayPal gezahlt.",
        "done_personal": "Danke. Es geht vollständig an {name} und wird am Ende der Schicht direkt auf das PayPal-Konto "
                         "gezahlt.",
        "confirming": "PayPal bestätigt Ihr Trinkgeld von {amount}. Sie können diese Seite schließen.",
        "not_found": "Wir konnten dieses Trinkgeld nicht finden. Falls Sie belastet wurden, kommt Ihre PayPal-Quittung.",
        "another": "Noch ein Trinkgeld geben", "close_page": "Sie können diese Seite schließen.",
    },
    "it": {
        "title": "Lascia una mancia", "thanks": "Grazie", "table": "Tavolo",
        "pool_note": "Grazie. La tua mancia viene divisa con tutto il team che si è preso cura di te oggi.",
        "who_personal": "A chi vuoi lasciare la mancia?", "who_both": "Per chi è questa mancia?",
        "whole_team": "Tutto il team",
        "note_both": "Le mance per tutto il team vengono divise in modo equo. Una mancia per una persona va tutta a lei.",
        "note_personal": "Tutta la tua mancia va alla persona che scegli.",
        "cancelled": "Nessuna mancia addebitata. Scegli un importo per riprovare.",
        "bill": "Il tuo conto", "tip_percent": "Percentuale della mancia", "tip_amount": "Importo della mancia",
        "other": "Altro importo",
        "nobody": "Al momento nessuno riceve mance qui. Chiedi al personale.",
        "loading": "Caricamento di PayPal…",
        "guest_note": "I pagamenti sono gestiti da PayPal. Il locale non vede mai i dati della tua carta.",
        "language": "Lingua", "privacy": "Privacy",
        "err_bill": "Inserisci il conto per lasciare una percentuale, oppure scrivi un importo.",
        "err_amount": "Scegli l'importo della mancia.", "err_who": "Scegli a chi vuoi lasciare la mancia.",
        "err_start": "Impossibile avviare il pagamento.", "completing": "Completamento della mancia…",
        "err_generic": "Qualcosa è andato storto.", "pay_cancelled": "Pagamento annullato. Puoi riprovare.",
        "err_paypal": "Errore PayPal. Riprova.",
        "err_load": "Impossibile caricare PayPal. Aggiorna la pagina.",
        "tip": "Mancia", "paid": "PAGATO",
        "done_note": "Grazie. A fine turno viene divisa tra tutti quelli che hanno lavorato e pagata direttamente sul "
                     "loro PayPal.",
        "done_personal": "Grazie. Va tutta a {name}, pagata direttamente sul suo PayPal a fine turno.",
        "confirming": "PayPal sta confermando la tua mancia di {amount}. Puoi chiudere questa pagina.",
        "not_found": "Non troviamo questa mancia. Se ti è stato addebitato, la ricevuta PayPal è in arrivo.",
        "another": "Lascia un'altra mancia", "close_page": "Puoi chiudere questa pagina.",
    },
    "pt": {
        "title": "Deixar uma gorjeta", "thanks": "Obrigado", "table": "Mesa",
        "pool_note": "Obrigado. A sua gorjeta é dividida com toda a equipa que o atendeu hoje.",
        "who_personal": "A quem quer dar a gorjeta?", "who_both": "Para quem é esta gorjeta?",
        "whole_team": "Toda a equipa",
        "note_both": "As gorjetas para toda a equipa são divididas de forma justa. Uma gorjeta para uma pessoa vai "
                     "inteira para ela.",
        "note_personal": "Toda a sua gorjeta vai para a pessoa que escolher.",
        "cancelled": "Nenhuma gorjeta foi cobrada. Escolha um valor para tentar de novo.",
        "bill": "A sua conta", "tip_percent": "Percentagem da gorjeta", "tip_amount": "Valor da gorjeta",
        "other": "Outro valor",
        "nobody": "De momento ninguém está a receber gorjetas aqui. Pergunte à equipa.",
        "loading": "A carregar o PayPal…",
        "guest_note": "Os pagamentos são processados pelo PayPal. O estabelecimento nunca vê os dados do seu cartão.",
        "language": "Idioma", "privacy": "Privacidade",
        "err_bill": "Introduza a sua conta para dar uma percentagem, ou escreva um valor.",
        "err_amount": "Escolha o valor da gorjeta.", "err_who": "Escolha a quem quer dar a gorjeta.",
        "err_start": "Não foi possível iniciar o pagamento.", "completing": "A concluir a sua gorjeta…",
        "err_generic": "Algo correu mal.", "pay_cancelled": "Pagamento cancelado. Pode tentar de novo.",
        "err_paypal": "Erro do PayPal. Tente de novo.",
        "err_load": "Não foi possível carregar o PayPal. Atualize a página.",
        "tip": "Gorjeta", "paid": "PAGO",
        "done_note": "Obrigado. No fim do turno é dividida entre todos os que trabalharam e paga diretamente no PayPal "
                     "de cada um.",
        "done_personal": "Obrigado. Vai inteira para {name}, paga diretamente no PayPal no fim do turno.",
        "confirming": "O PayPal está a confirmar a sua gorjeta de {amount}. Pode fechar esta página.",
        "not_found": "Não encontrámos esta gorjeta. Se foi cobrado, o recibo do PayPal está a caminho.",
        "another": "Deixar outra gorjeta", "close_page": "Pode fechar esta página.",
    },
    "ro": {
        "title": "Lasă un bacșiș", "thanks": "Mulțumim", "table": "Masa",
        "pool_note": "Mulțumim. Bacșișul tău este împărțit cu toată echipa care te-a servit azi.",
        "who_personal": "Cui vrei să-i lași bacșiș?", "who_both": "Pentru cine este acest bacșiș?",
        "whole_team": "Toată echipa",
        "note_both": "Bacșișul pentru toată echipa se împarte corect. Bacșișul pentru o persoană merge integral la ea.",
        "note_personal": "Tot bacșișul merge la persoana pe care o alegi.",
        "cancelled": "Nu s-a încasat niciun bacșiș. Alege o sumă ca să încerci din nou.",
        "bill": "Nota ta", "tip_percent": "Procent bacșiș", "tip_amount": "Suma bacșișului", "other": "Altă sumă",
        "nobody": "Momentan nimeni nu primește bacșiș aici. Întreabă personalul.",
        "loading": "Se încarcă PayPal…",
        "guest_note": "Plățile sunt procesate de PayPal. Localul nu vede niciodată datele cardului tău.",
        "language": "Limba", "privacy": "Confidențialitate",
        "err_bill": "Introdu nota ca să dai un procent, sau scrie o sumă.",
        "err_amount": "Alege suma bacșișului.", "err_who": "Alege cui vrei să-i lași bacșiș.",
        "err_start": "Plata nu a putut porni.", "completing": "Se finalizează bacșișul…",
        "err_generic": "Ceva n-a mers bine.", "pay_cancelled": "Plată anulată. Poți încerca din nou.",
        "err_paypal": "Eroare PayPal. Încearcă din nou.",
        "err_load": "PayPal nu s-a putut încărca. Reîncarcă pagina.",
        "tip": "Bacșiș", "paid": "PLĂTIT",
        "done_note": "Mulțumim. La finalul turei se împarte între toți cei care au lucrat și se plătește direct în "
                     "PayPal-ul lor.",
        "done_personal": "Mulțumim. Merge integral la {name}, plătit direct în PayPal la finalul turei.",
        "confirming": "PayPal confirmă bacșișul tău de {amount}. Poți închide pagina.",
        "not_found": "Nu am găsit acest bacșiș. Dacă ai fost taxat, chitanța PayPal e pe drum.",
        "another": "Mai lasă un bacșiș", "close_page": "Poți închide pagina.",
    },
}

# strings the page's script needs
JS_KEYS = ("err_bill", "err_amount", "err_who", "err_start", "completing", "err_generic", "pay_cancelled",
           "err_paypal", "err_load")


def pick(request):
    """The guest's language code."""
    asked = request.GET.get("lang", "")[:5].lower()
    if asked in LANGS:
        return asked
    saved = request.COOKIES.get(COOKIE, "")
    if saved in LANGS:
        return saved
    for part in request.META.get("HTTP_ACCEPT_LANGUAGE", "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return "en"


def strings(lang):
    return STRINGS.get(lang, STRINGS["en"])


def remember(response, request, lang):
    """Keep a language the guest picked by hand for the thank-you page and their next visit."""
    if request.GET.get("lang", "")[:5].lower() == lang:
        response.set_cookie(COOKIE, lang, max_age=180 * 24 * 3600, samesite="Lax", secure=request.is_secure())
    return response

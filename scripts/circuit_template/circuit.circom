pragma circom 2.0.0;

include "circomlib/circuits/comparators.circom";
include "circomlib/circuits/gates.circom";

template VerifyTransaction() {
    // Entrées pour une seule transaction
    signal input src;
    signal input srcBalance;
    signal input srcBalanceAfter;
    signal input dest;
    signal input destBalance;
    signal input destBalanceAfter;
    signal input amount;

    // Sortie : true si la transaction est valide
    signal output isValid;

    // 1. Vérification que la source a assez de fonds
    component checkSrcFunds = LessEqThan(64);
    checkSrcFunds.in[0] <== amount;
    checkSrcFunds.in[1] <== srcBalance;

    // 2. Vérification que le montant est positif
    component checkAmountPositive = GreaterThan(64);
    checkAmountPositive.in[0] <== amount;
    checkAmountPositive.in[1] <== 0;

    // 3. Vérification de la balance après transaction pour le wallet source
    signal computedSrcBalanceAfter;
    computedSrcBalanceAfter <== srcBalance - amount;
    component checkSrcBalanceAfter = IsEqual();
    checkSrcBalanceAfter.in[0] <== computedSrcBalanceAfter;
    checkSrcBalanceAfter.in[1] <== srcBalanceAfter;

    // 4. Vérification de la balance après transaction pour le wallet destination
    signal computedDestBalanceAfter;
    computedDestBalanceAfter <== destBalance + amount;
    component checkDestBalanceAfter = IsEqual();
    checkDestBalanceAfter.in[0] <== computedDestBalanceAfter;
    checkDestBalanceAfter.in[1] <== destBalanceAfter;

    // 5. Vérification que la source est différente de la destination
    component checkSrcNotEqualDest = IsEqual();
    checkSrcNotEqualDest.in[0] <== src;
    checkSrcNotEqualDest.in[1] <== dest;

    component checkSrcDifferentFromDest = NOT();
    checkSrcDifferentFromDest.in <== checkSrcNotEqualDest.out;

    // 6. Vérification finale
    component checkSrcFundsAndAmountPositive = AND();    
    checkSrcFundsAndAmountPositive.a <== checkSrcFunds.out;
    checkSrcFundsAndAmountPositive.b <== checkAmountPositive.out;

    component checkSrcBalanceAfterAndDestBalanceAfter = AND();
    checkSrcBalanceAfterAndDestBalanceAfter.a <== checkSrcBalanceAfter.out;
    checkSrcBalanceAfterAndDestBalanceAfter.b <== checkDestBalanceAfter.out;

    component allChecksOK = AND();
    allChecksOK.a <== checkSrcFundsAndAmountPositive.out * checkSrcBalanceAfterAndDestBalanceAfter.out;
    allChecksOK.b <== checkSrcDifferentFromDest.out;

    isValid <== allChecksOK.out;
}

// Circuit principal pour vérifier un nombre variable de transactions
template VerifyNTransactions(n) {
    // Entrées : tableaux pour n transactions
    signal input src[n];
    signal input srcBalance[n];
    signal input srcBalanceAfter[n];
    signal input dest[n];
    signal input destBalance[n];
    signal input destBalanceAfter[n];
    signal input amount[n];

    // Sortie : true si toutes les transactions sont valides
    signal output isValid;

    // Tableau pour stocker la validité de chaque transaction
    signal isValidTransactions[n];

    // Instances du circuit VerifyTransaction pour chaque transaction
    component verifyTransactions[n];
    for (var i = 0; i < n; i++) {
        verifyTransactions[i] = VerifyTransaction();
        verifyTransactions[i].src <== src[i];
        verifyTransactions[i].srcBalance <== srcBalance[i];
        verifyTransactions[i].srcBalanceAfter <== srcBalanceAfter[i];
        verifyTransactions[i].dest <== dest[i];
        verifyTransactions[i].destBalance <== destBalance[i];
        verifyTransactions[i].destBalanceAfter <== destBalanceAfter[i];
        verifyTransactions[i].amount <== amount[i];
        
        isValidTransactions[i] <== verifyTransactions[i].isValid;
    }

    // Composant récursif pour combiner toutes les valeurs de isValidTransactions avec des AND
    component allValid = RecursiveAnd(n);
    for (var i = 0; i < n; i++) {
        allValid.inputs[i] <== isValidTransactions[i];
    }

    // Résultat final : true si toutes les transactions sont valides
    isValid <== allValid.out;
}

// Template récursif pour calculer le AND de n entrées
template RecursiveAnd(n) {
    // Entrées
    signal input inputs[n];
    // Sortie
    signal output out;

    if (n == 1) {
        // Si n est 1, on retourne simplement la première entrée
        out <== inputs[0];
    } else if (n == 2) {
        // Si n est 2, on effectue directement le AND entre les deux entrées
        component andGate = AND();
        andGate.a <== inputs[0];
        andGate.b <== inputs[1];
        out <== andGate.out;
    } else {
        // Si n est plus grand que 2, on divise en deux sous-groupes pour la récursion
        component left = RecursiveAnd(n / 2);
        component right = RecursiveAnd(n - n / 2);

        // Connecter les entrées pour la partie gauche et la partie droite
        for (var i = 0; i < n / 2; i++) {
            left.inputs[i] <== inputs[i];
        }
        for (var i = n / 2; i < n; i++) {
            right.inputs[i - n / 2] <== inputs[i];
        }

        // Composant AND pour combiner les deux sous-parties
        component combine = AND();
        combine.a <== left.out;
        combine.b <== right.out;
        out <== combine.out;
    }
}

// Exemple d'instance principale avec un nombre de transactions fixe, par exemple 10
component main = VerifyNTransactions(XXX);
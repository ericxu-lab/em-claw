"""RELION 5.0 metadata-label type snapshot used for integrity validation.
Generated from RELION 5.0 commit cad71bf6d67979a30c7a78b6567eae55db2c11b1:
https://github.com/3dem/relion/blob/cad71bf6d67979a30c7a78b6567eae55db2c11b1/src/metadata_label.h
Source SHA-256: e950599d2c3509170821d7b2a4809a5ec2294ed3073d9aa4af8e4c1def2b5f3b
Only label names and their declared scalar/vector types are retained.
"""

from __future__ import annotations

from typing import Final

_LABELS_BY_TYPE: Final[dict[str, str]] = {
    "string": """
        rlnAreaName rlnBodyMaskName rlnBodyReferenceName rlnBodyStarFile rlnComment rlnCtfImage
        rlnCtfPowerSpectrum rlnEtomoDirectiveFile rlnExperimentalDataStarFile rlnExtReconsDataImag
        rlnExtReconsDataReal rlnExtReconsResult rlnExtReconsResultStarfile rlnExtReconsWeight
        rlnFourierMask rlnGradMoment1 rlnGradMoment2 rlnGroupName rlnImageName rlnImageOriginalName
        rlnJobOptionDefaultValue rlnJobOptionDirectoryDefault rlnJobOptionFilePattern
        rlnJobOptionGUILabel rlnJobOptionHelpText rlnJobOptionMenuOptions rlnJobOptionValue
        rlnJobOptionVariable rlnJobTypeLabel rlnLocalSymmetryFile rlnMaskName rlnMicrographCoordinates
        rlnMicrographDefectFile rlnMicrographGainName rlnMicrographMetadata rlnMicrographMovieName
        rlnMicrographName rlnMicrographNameEven rlnMicrographNameNoDW rlnMicrographNameOdd
        rlnModelStarFile rlnModelStarFile2 rlnMtfFileName rlnOpticsGroupName rlnOpticsStarFile
        rlnOrientSamplingStarFile rlnOriginalParticleName rlnOutputRootName rlnParticleName
        rlnPipeLineEdgeFromNode rlnPipeLineEdgeProcess rlnPipeLineEdgeToNode rlnPipeLineNodeName
        rlnPipeLineNodeTypeLabel rlnPipeLineProcessAlias rlnPipeLineProcessName
        rlnPipeLineProcessStatusLabel rlnPipeLineProcessTypeLabel rlnPostprocessedMap
        rlnPostprocessedMapMasked rlnReconstructImageName rlnReferenceImage rlnSchemeBooleanVariableName
        rlnSchemeCurrentNodeName rlnSchemeEdgeBooleanVariable rlnSchemeEdgeInputNodeName
        rlnSchemeEdgeOutputNodeName rlnSchemeEdgeOutputNodeNameIfTrue rlnSchemeEmailAddress
        rlnSchemeFloatVariableName rlnSchemeJobMode rlnSchemeJobName rlnSchemeJobNameOriginal
        rlnSchemeName rlnSchemeOperatorInput1 rlnSchemeOperatorInput2 rlnSchemeOperatorName
        rlnSchemeOperatorOutput rlnSchemeOperatorType rlnSchemeOriginalStartNodeName
        rlnSchemeStringVariableName rlnSchemeStringVariableResetValue rlnSchemeStringVariableValue
        rlnSgdStepsizeScheme rlnSolventMask2Name rlnSolventMaskName rlnStarFileMovieParticles
        rlnSubImageStack rlnSubImageStarFile rlnSymmetryGroup rlnTau2FudgeScheme rlnTauSpectrumName
        rlnTomoDeformationType rlnTomoDenoisedTomogram rlnTomoFiducialsStarFile rlnTomoImportCtfFindFile
        rlnTomoImportCtfPlotterFile rlnTomoImportCulledFile rlnTomoImportImodDir rlnTomoImportOrderList
        rlnTomoImportParticleFile rlnTomoManifoldType rlnTomoManifoldsFile rlnTomoName
        rlnTomoParticleName rlnTomoParticlesFile rlnTomoReconstructedTomogram
        rlnTomoReconstructedTomogramHalf1 rlnTomoReconstructedTomogramHalf2 rlnTomoReferenceFscFile
        rlnTomoReferenceMap1File rlnTomoReferenceMap2File rlnTomoReferenceMaskFile rlnTomoTiltMovieFile
        rlnTomoTiltSeriesName rlnTomoTiltSeriesStarFile rlnTomoTomogramsFile rlnTomoTrajectoriesFile
        rlnTomogramHProjectionalf2 rlnTomogramProjection rlnTomogramProjectionHalf1
        rlnUnfilteredMapHalf1 rlnUnfilteredMapHalf2
    """,
    "int": """
        randomSeed rlnAdaptiveOversampleOrder rlnAreaId rlnAutoLocalSearchesHealpixOrder
        rlnAverageNrOfFrames rlnBeamTiltClass rlnBodyKeepFixed rlnBodyRotateRelativeTo rlnClassIndex
        rlnClassNumber rlnCoarseImageSize rlnCurrentImageSize rlnCurrentIteration rlnDataDimensionality
        rlnDataType rlnEERGrouping rlnEERUpsampling rlnFourierSpaceInterpolator rlnGradEmIters
        rlnGradSubsetOrder rlnGradSuspendFinerSamplingIter rlnGradSuspendLocalSamplingIter
        rlnGroupNrParticles rlnGroupNumber rlnHasLargeSizeIncreaseIterationsAgo rlnHealpixOrder
        rlnHealpixOrderOriginal rlnHelicalTubeID rlnImageDimensionality rlnImageId rlnImageSize
        rlnImageSizeX rlnImageSizeY rlnImageSizeZ rlnIncrementImageSize rlnIsSelected rlnJobType
        rlnJoboptionType rlnMaxNumberOfPooledParticles rlnMaximumCoarseImageSize
        rlnMaximumSignificantPoses rlnMicrographEndFrame rlnMicrographFrameNumber rlnMicrographId
        rlnMicrographStartFrame rlnMinRadiusNnInterpolation rlnMotionModelCoeffsIdx
        rlnMotionModelVersion rlnMovieFrameNumber rlnMovieFramesRunningAverage rlnNrBodies rlnNrClasses
        rlnNrGroups rlnNrHelicalAsymUnits rlnNrHelicalNStart rlnNrOfFrames rlnNrOfSignificantSamples
        rlnNrOpticsGroups rlnNumberOfIterWithoutChangingAssignments rlnNumberOfIterWithoutResolutionGain
        rlnNumberOfIterations rlnOpticsGroup rlnOpticsGroupNrParticles rlnOpticsGroupNumber
        rlnOrientationalPriorMode rlnOrientationsID rlnOriginalImageSize rlnParticleId rlnParticleNumber
        rlnParticleSelectionType rlnPipeLineJobCounter rlnPipeLineNodeType rlnPipeLineNodeTypeLabelDepth
        rlnPipeLineProcessStatus rlnPipeLineProcessType rlnProteinArea rlnRadiusMaskExpImages
        rlnRadiusMaskMap rlnRandomSeed rlnRandomSubset rlnReferenceDimensionality rlnSchemeEdgeNumber
        rlnSelected rlnSgdFinalSubsetSize rlnSgdInitialSubsetSize rlnSgdMaxSubsets
        rlnSgdSigma2FudgeHalflife rlnSgdSubsetSize rlnSgdWriteEverySubset rlnSmallestChangesClasses
        rlnSolventArea rlnSortedIndex rlnSpectralIndex rlnTomoDeformationGridSizeX
        rlnTomoDeformationGridSizeY rlnTomoFrameCount rlnTomoManifoldIndex rlnTomoParticleId
        rlnTomoSizeX rlnTomoSizeY rlnTomoSizeZ rlnTomoTiltMovieFrameCount rlnTomoTiltMovieIndex
        rlnWidthMaskEdge
    """,
    "bool": """
        rlnAnglePsiFlip rlnBlushNoSpectralTrailing rlnCtfDataAreCtfCorrected
        rlnCtfDataAreCtfPremultiplied rlnCtfDataArePhaseFlipped rlnDoAutoRefine rlnDoAutoSampling
        rlnDoBlush rlnDoCenterClasses rlnDoCorrectCtf rlnDoCorrectMagnification rlnDoCorrectNorm
        rlnDoCorrectScale rlnDoExternalReconstruct rlnDoFastSubsetOptimisation rlnDoGradientRefine
        rlnDoHelicalRefine rlnDoIgnoreCtfUntilFirstPeak rlnDoMapEstimation rlnDoOnlyFlipCtfPhases
        rlnDoRealignMovies rlnDoSkipAlign rlnDoSkipRotate rlnDoSolventFlattening
        rlnDoSolventFscCorrection rlnDoSplitRandomHalves rlnDoStochasticEM
        rlnDoStochasticGradientDescent rlnDoZeroMask rlnEnabled rlnFixSigmaNoiseEstimates
        rlnFixSigmaOffsetEstimates rlnFixTauEstimates rlnGradHasConverged rlnHasConverged
        rlnHasHighFscAtResolLimit rlnHelicalKeepTiltPriorFixed rlnHelicalSymmetryLocalRefinement
        rlnIgnoreHelicalSymmetry rlnIs3DSampling rlnIs3DTranslationalSampling rlnIsFlip rlnIsHelix
        rlnJobIsContinue rlnJobIsTomo rlnRefsAreCtfCorrected rlnSchemeBooleanVariableResetValue
        rlnSchemeBooleanVariableValue rlnSchemeEdgeIsFork rlnSchemeJobHasStarted rlnSgdSkipAnneal
        rlnTomoSubTomosAre2DStacks rlnUseTooCoarseSampling
    """,
    "double": """
        rlnAccumMotionEarly rlnAccumMotionLate rlnAccumMotionTotal rlnAccuracyRotations
        rlnAccuracyTranslations rlnAccuracyTranslationsAngst rlnAdaptiveOversampleFraction
        rlnAmplitudeContrast rlnAmplitudeCorrelationMaskedMaps rlnAmplitudeCorrelationUnmaskedMaps
        rlnAnglePsi rlnAnglePsiFlipRatio rlnAnglePsiPrior rlnAngleRot rlnAngleRotFlipRatio
        rlnAngleRotPrior rlnAngleTilt rlnAngleTiltPrior rlnAngstromResolution rlnAreTomoTiltAxisScore
        rlnAreTomoTiltangleOffset rlnAutopickFigureOfMerit rlnAvailableMemory rlnAveragePmax
        rlnAverageValue rlnBeamTiltX rlnBeamTiltY rlnBestResolutionThusFar rlnBfactorUsedForSharpening
        rlnBodyRotateDirectionX rlnBodyRotateDirectionY rlnBodyRotateDirectionZ rlnBodySigmaAngles
        rlnBodySigmaOffset rlnBodySigmaOffsetAngst rlnBodySigmaPsi rlnBodySigmaRot rlnBodySigmaTilt
        rlnCenteredCoordinateXAngst rlnCenteredCoordinateYAngst rlnCenteredCoordinateZAngst
        rlnChangesOptimalClasses rlnChangesOptimalOffsets rlnChangesOptimalOrientations
        rlnChromaticAberration rlnCircleMaskedKurt rlnCircleMaskedMean rlnCircleMaskedSkew
        rlnCircleMaskedStddev rlnClassDistribution rlnClassPriorOffsetX rlnClassPriorOffsetY
        rlnClassScore rlnConvergenceCone rlnCoordinateX rlnCoordinateY rlnCoordinateZ
        rlnCorrectedFourierShellCorrelationPhaseRandomizedMaskedMaps rlnCorrelationFitGuinierPlot
        rlnCtfAstigmatism rlnCtfBfactor rlnCtfBfactorPerElectronDose rlnCtfFigureOfMerit
        rlnCtfIceRingDensity rlnCtfMaxResolution rlnCtfScalefactor rlnCtfValidationScore rlnCtfValue
        rlnCurrentResolution rlnDefocusAngle rlnDefocusU rlnDefocusV rlnDetectorPixelSize
        rlnDiff2RandomHalves rlnDifferentialPhaseResidualMaskedMaps
        rlnDifferentialPhaseResidualUnmaskedMaps rlnEdgeSignal rlnEnergyLoss rlnEstimatedResolution
        rlnFftKurt rlnFftMean rlnFftSkew rlnFftStddev rlnFinalResolution rlnFittedInterceptGuinierPlot
        rlnFittedSlopeGuinierPlot rlnFourierCompleteness rlnFourierShellCorrelation
        rlnFourierShellCorrelationCorrected rlnFourierShellCorrelationMaskedMaps
        rlnFourierShellCorrelationParticleMaskFraction rlnFourierShellCorrelationParticleMolWeight
        rlnFourierShellCorrelationUnmaskedMaps rlnGoldStandardFsc rlnGradCurrentStepsize
        rlnGroupScaleCorrection rlnHelicalCentralProportion rlnHelicalMaskTubeInnerDiameter
        rlnHelicalMaskTubeOuterDiameter rlnHelicalOffsetStep rlnHelicalRise rlnHelicalRiseInitial
        rlnHelicalRiseInitialStep rlnHelicalRiseMax rlnHelicalRiseMin rlnHelicalSigmaDistance
        rlnHelicalTrackLength rlnHelicalTrackLengthAngst rlnHelicalTubePitch rlnHelicalTwist
        rlnHelicalTwistInitial rlnHelicalTwistInitialStep rlnHelicalTwistMax rlnHelicalTwistMin
        rlnHighresLimitExpectation rlnHighresLimitSGD rlnIMODLeaveOutError rlnIMODResidualErrorMean
        rlnIMODResidualErrorStddev rlnImagePixelSize rlnImageWeight rlnInnerCircleKurt
        rlnInnerCircleMean rlnInnerCircleSkew rlnInnerCircleStddev rlnJobOptionSliderMax
        rlnJobOptionSliderMin rlnJobOptionSliderStep rlnJobScore rlnJoinHalvesUntilThisResolution
        rlnKullbackLeiblerDivergence rlnKurtosisExcessValue rlnLensStability rlnLogAmplitudesIntercept
        rlnLogAmplitudesMTFCorrected rlnLogAmplitudesOriginal rlnLogAmplitudesSharpened
        rlnLogAmplitudesWeighted rlnLogLikeliContribution rlnLogLikelihood rlnLongitudinalDisplacement
        rlnLowpassFilteredImageMax rlnLowpassFilteredImageMean rlnLowpassFilteredImageMin
        rlnLowpassFilteredImageStddev rlnLowresLimitExpectation rlnMagMat00 rlnMagMat01 rlnMagMat10
        rlnMagMat11 rlnMagnification rlnMagnificationCorrection rlnMagnificationSearchRange
        rlnMagnificationSearchStep rlnMatrix_1_1 rlnMatrix_1_2 rlnMatrix_1_3 rlnMatrix_2_1 rlnMatrix_2_2
        rlnMatrix_2_3 rlnMatrix_3_1 rlnMatrix_3_2 rlnMatrix_3_3 rlnMaxValueProbDistribution
        rlnMaximumValue rlnMicrographBinning rlnMicrographDoseRate rlnMicrographIceThickness
        rlnMicrographOriginalPixelSize rlnMicrographPixelSize rlnMicrographPreExposure
        rlnMicrographShiftX rlnMicrographShiftY rlnMicrographTiltAngle rlnMicrographTiltAxisDirection
        rlnMicrographTiltAxisOutOfPlane rlnMinimumValue rlnMolecularWeight rlnMotionModelCoeff
        rlnMtfValue rlnNormCorrection rlnNormCorrectionAverage rlnOffsetRange rlnOffsetRangeOriginal
        rlnOffsetRangeX rlnOffsetRangeY rlnOffsetRangeZ rlnOffsetStep rlnOffsetStepOriginal
        rlnOrientationDistribution rlnOriginX rlnOriginXAngst rlnOriginXPrior rlnOriginXPriorAngst
        rlnOriginY rlnOriginYAngst rlnOriginYPrior rlnOriginYPriorAngst rlnOriginZ rlnOriginZAngst
        rlnOriginZPrior rlnOriginZPriorAngst rlnOverallAccuracyRotations rlnOverallAccuracyTranslations
        rlnOverallAccuracyTranslationsAngst rlnOverallFourierCompleteness rlnPaddingFactor
        rlnParticleBoxFractionMolecularWeight rlnParticleBoxFractionSolventMask rlnParticleDiameter
        rlnParticleFigureOfMerit rlnParticleNr rlnParticleSelectZScore rlnPerFrameCumulativeWeight
        rlnPerFrameRelativeWeight rlnPhaseShift rlnPixelSize rlnPredictedClassScore rlnProteinCAR
        rlnProteinEntropy rlnProteinKurt rlnProteinMean rlnProteinSkew rlnProteinStddev rlnProteinSum
        rlnPsiStep rlnPsiStepOriginal rlnRandomiseFrom rlnReferenceSigma2 rlnReferenceSpectralPower
        rlnReferenceTau2 rlnRelativeResolution rlnRelativeSignalIntensity rlnResolution
        rlnResolutionInversePixel rlnResolutionSquared rlnRingKurt rlnRingMean rlnRingSkew rlnRingStddev
        rlnSamplingPerturbFactor rlnSamplingPerturbInstance rlnSamplingRate rlnSamplingRateX
        rlnSamplingRateY rlnSamplingRateZ rlnScatteredSignal rlnSchemeFloatVariableResetValue
        rlnSchemeFloatVariableValue rlnSgdClassInactivityThreshold rlnSgdFinalIterationsFraction
        rlnSgdFinalResolution rlnSgdInitialIterationsFraction rlnSgdInitialResolution
        rlnSgdMinimumResolution rlnSgdMuFactor rlnSgdSigma2FudgeInitial rlnSgdStepsize rlnSigma2Noise
        rlnSigmaOffsets rlnSigmaOffsetsAngst rlnSigmaPriorPsiAngle rlnSigmaPriorRotAngle
        rlnSigmaPriorTiltAngle rlnSignalToNoiseRatio rlnSkewnessValue rlnSmallestChangesOffsets
        rlnSmallestChangesOrientations rlnSolventEntropy rlnSolventKurt rlnSolventMean rlnSolventSkew
        rlnSolventStddev rlnSolventSum rlnSpectralOrientabilityContribution rlnSphericalAberration
        rlnSsnrMap rlnStandardDeviationValue rlnTau2FudgeArg rlnTau2FudgeFactor rlnTiltAngleLimit
        rlnTomoDefocusSlope rlnTomoHand rlnTomoIceNormalX rlnTomoIceNormalY rlnTomoIceNormalZ
        rlnTomoImportFractionalDose rlnTomoImportOffsetX rlnTomoImportOffsetY rlnTomoImportOffsetZ
        rlnTomoNominalDefocus rlnTomoNominalStageTiltAngle rlnTomoNominalTiltAxisAngle
        rlnTomoRelativeIceThickness rlnTomoRelativeLuminance rlnTomoSubtomogramBinning
        rlnTomoSubtomogramPsi rlnTomoSubtomogramRot rlnTomoSubtomogramTilt rlnTomoTempPredSquared
        rlnTomoTempPredTimesObs rlnTomoTiltSeriesPixelSize rlnTomoTomogramBinning
        rlnTomoTomogramThickness rlnTomoXShiftAngst rlnTomoXTilt rlnTomoYShiftAngst rlnTomoYTilt
        rlnTomoZRot rlnTotalEntropy rlnTransversalDisplacement rlnVoltage rlnWeightedResolution
    """,
    "double_vector": """
        rlnEvenZernike rlnGranulo rlnLBP rlnNormalizedFeatureVector rlnOddZernike rlnProteinHaralick
        rlnProteinLBP rlnSolventHaralick rlnSolventLBP rlnTomoDeformationCoefficients
        rlnTomoManifoldParams rlnTomoProjW rlnTomoProjX rlnTomoProjY rlnTomoProjZ rlnZernikeMoments
    """,
    "int_vector": """
        rlnTomoVisibleFrames
    """,
    "unknown": """
        rlnUnknownLabel
    """,
}

RELION_METADATA_LABEL_TYPES: Final[dict[str, str]] = {
    label: label_type
    for label_type, labels in _LABELS_BY_TYPE.items()
    for label in labels.split()
}
RELION_METADATA_LABEL_TYPES["rlnKullbackLeibnerDivergence"] = "double"

RELION_METADATA_CANONICAL_LABELS: Final[dict[str, str]] = {
    "rlnKullbackLeibnerDivergence": "rlnKullbackLeiblerDivergence",
}

RELION_WRITER_SPECIAL_LOOP_LABELS: Final[frozenset[str]] = frozenset({
    "rlnComment",
    "rlnSortedIndex",
})
